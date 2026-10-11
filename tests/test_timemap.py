"""Piège idleTimeLimit.

Constat (asciinema-player 3.17, dist/core-*.js, prepareRecording/timeLimiter) :
  * idleTimeLimit = opts.idleTimeLimit ?? header.idle_time_limit  -> le header
    suffit à activer la compression ;
  * les temps des events sont réécrits AVANT lecture : seek(), getCurrentTime()
    et getDuration() sont en temps COMPRESSÉ ;
  * les `markers` passés en option ne sont PAS compressés (ajoutés après).

Donc : le serveur retire idle_time_limit du header servi, compresse lui-même
quand on le demande, et fournit la table de correspondance (knots) pour
convertir les offsets (en temps d'origine) en temps du player.
"""
import json
from pathlib import Path

import pytest

from castdeck import asciicast as ac

FIX = Path(__file__).parent / "fixtures"


def player_time_limiter(times, limit):
    """Copie conforme du timeLimiter d'asciinema-player (en secondes)."""
    previous = 0.0
    shift = 0.0
    out = []
    for t in times:
        delay = t - previous
        delta = delay - limit
        previous = t
        if delta > 0:
            shift += delta
        out.append(t - shift)
    return out


CASES = [
    ([0.5, 1.0, 1.2, 9.0, 9.1, 30.0, 30.5], 2.0),
    ([5.0, 5.1, 5.2], 1.0),          # trou initial
    ([0.1, 0.2, 0.3], 2.0),          # aucune compression
    ([3.0, 10.0, 10.0, 25.0], 0.5),  # events simultanés
]


@pytest.mark.parametrize("times,limit", CASES)
def test_compress_matches_player(times, limit):
    assert list(ac.compress_times(times, limit)) == pytest.approx(
        player_time_limiter(times, limit)
    )


@pytest.mark.parametrize("times,limit", CASES)
def test_knots_map_events_like_player(times, limit):
    knots = ac.time_knots(times, limit)
    expected = player_time_limiter(times, limit)
    for t, d in zip(times, expected):
        assert ac.orig_to_display(knots, t) == pytest.approx(d)
        # au temps affiché d'un event, le player est bien sur cet event
        assert ac.display_to_orig(knots, d) == pytest.approx(t)


@pytest.mark.parametrize("times,limit", CASES)
def test_mapping_monotonic_and_bounded(times, limit):
    knots = ac.time_knots(times, limit)
    xs = [i * 0.05 for i in range(int(times[-1] * 20) + 40)]
    ys = [ac.orig_to_display(knots, x) for x in xs]
    assert all(b >= a - 1e-9 for a, b in zip(ys, ys[1:]))
    for x, y in zip(xs, ys):
        assert y <= x + 1e-9
        # aller-retour : on retombe à <= limit secondes après, jamais avant
        back = ac.display_to_orig(knots, y)
        assert back >= x - 1e-9


def test_header_served_without_idle_time_limit():
    # Le fixture v2 a idle_time_limit=2 : s'il passait au player, celui-ci
    # compresserait silencieusement et nos offsets seraient faux.
    lines = list(ac.to_v2_lines(FIX / "small_v2.cast"))
    header = json.loads(lines[0])
    assert "idle_time_limit" not in header
    lines3 = list(ac.to_v2_lines(FIX / "small_v3.asciinema"))
    assert "idle_time_limit" not in json.loads(lines3[0])


def test_served_compressed_cast_consistent_with_knots():
    path = FIX / "small_v2.cast"
    orig = [t for t, _, _ in ac.iter_events(path)]
    served = [json.loads(l)[0] for l in list(ac.to_v2_lines(path, idle_limit=2.0))[1:]]
    knots = ac.time_knots(orig, 2.0)
    assert served == pytest.approx([ac.orig_to_display(knots, t) for t in orig])


def test_shared_cases_for_frontend():
    """Écrit les cas de référence utilisés aussi par le test TS du front
    (frontend/src/timemap.test.ts) : les deux implémentations doivent
    produire exactement les mêmes valeurs."""
    out = []
    for times, limit in CASES:
        knots = ac.time_knots(times, limit)
        probes = [i * 0.25 for i in range(int(times[-1] * 4) + 8)]
        out.append({
            "limit": limit,
            "knots": knots,
            "probes": probes,
            "display": [ac.orig_to_display(knots, p) for p in probes],
            "back": [ac.display_to_orig(knots, ac.orig_to_display(knots, p)) for p in probes],
        })
    target = FIX / "timemap_cases.json"
    data = json.dumps(out, indent=1)
    if not target.exists() or target.read_text() != data:
        target.write_text(data)
