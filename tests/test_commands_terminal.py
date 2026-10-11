from datetime import datetime, timezone
from pathlib import Path

from castdeck import commands as cm
from castdeck import terminal
from castdeck.redact import redact

FIX = Path(__file__).parent / "fixtures"


def test_parse_commands_log_with_tz(tmp_path):
    p = tmp_path / "commands.log"
    p.write_text(
        "2026-10-10 13:00:02 [cli] nmap -sV 10.10.10.5\n"
        "2026-10-10 13:00:14 [%3] cat <<EOF\n"
        "suite multi-ligne\n"
        "ligne pourrie\n"
        "2026-10-10 13:00:20 [] whoami\n"
    )
    cmds = cm.parse_commands_log(p, cm.resolve_tz("Europe/Paris"))  # UTC+2 en octobre
    assert [c.ctx for c in cmds] == ["cli", "%3", "cli"]
    assert cmds[0].abs_time == datetime(2026, 10, 10, 11, 0, 2, tzinfo=timezone.utc).timestamp()
    assert cmds[1].cmd == "cat <<EOF\nsuite multi-ligne\nligne pourrie"


def test_attach_to_sessions():
    base = 1791630000.0
    spans = [cm.Span("a", base, base + 15), cm.Span("b", base + 3600, base + 3613)]
    cmds = [cm.Command(base + 2, "cli", "nmap"), cm.Command(base + 3602, "cli", "whoami"),
            cm.Command(base + 500, "cli", "orpheline"), cm.Command(base + 15.5, "cli", "bord")]
    cm.attach(cmds, spans)
    assert [(c.session_id, c.offset) for c in cmds] == [("a", 2.0), ("b", 2.0), (None, None), ("a", 15.0)]


def test_replay_snapshots_and_heuristic_commands():
    snaps, detected = terminal.replay(FIX / "small_v2.cast", interval=5.0)
    assert [d.cmd for d in detected] == ["nmap -sV 10.10.10.5", "cat /etc/passwd"]
    assert [d.offset for d in detected] == [2.0, 14.0]
    text = "\n".join(s.text for s in snaps)
    assert "22/tcp open  ssh" in text
    assert "root:x:0:0" in text
    # pas de doublon : la ligne nmap restée à l'écran n'est indexée qu'une fois
    assert text.count("Starting Nmap 7.94") == 1


def test_replay_resize_and_scrolled_lines():
    snaps, _ = terminal.replay(FIX / "resize.cast", interval=100)
    text = "\n".join(s.text for s in snaps)
    assert "x" * 100 in text  # n'est possible qu'après le resize à 120 colonnes
    assert "hello" in text and "fin" in text


def test_scrolled_lines_are_indexed(tmp_path):
    import json
    p = tmp_path / "scroll.cast"
    with open(p, "w") as f:
        f.write(json.dumps({"version": 2, "width": 40, "height": 5, "timestamp": 0}) + "\n")
        f.write(json.dumps([0.1, "o", "".join(f"ligne {i}\r\n" for i in range(50))]) + "\n")
    snaps, _ = terminal.replay(p, interval=5)
    text = "\n".join(s.text for s in snaps)
    assert all(f"ligne {i}" in text.split("\n") for i in range(50))


def test_screen_at():
    assert "22/tcp open" in terminal.screen_at(FIX / "small_v2.cast", 4.0)
    assert "root:x" not in terminal.screen_at(FIX / "small_v2.cast", 4.0)


def test_redact_preserves_length():
    cases = {
        "nxc smb dc -u admin -p 'S3cr3t!'": "nxc smb dc -u admin -p *********",
        "curl 'x?password=hunter2&a=1'": "curl 'x?password=*******&a=1'",
        "secretsdump -hashes aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0 x":
            "secretsdump -hashes " + "*" * 65 + " x",
        "Authorization: Bearer eyJhbGciOi.eyJzdWIiOiIx.sig_ABC-123": "Authorization: Bearer " + "*" * len("eyJhbGciOi.eyJzdWIiOiIx.sig_ABC-123"),
        "ls -la /tmp": "ls -la /tmp",
    }
    for src, expected in cases.items():
        assert redact(src) == expected
        assert len(redact(src)) == len(src)


def test_flood_is_capped_and_streamed(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(terminal, "MAX_CHARS_PER_TICK", 2000)
    p = tmp_path / "flood.cast"
    with open(p, "w") as f:
        f.write(json.dumps({"version": 2, "width": 80, "height": 5, "timestamp": 0}) + "\n")
        f.write(json.dumps([0.1, "o", "".join(f"ligne {i:05d} " + "x" * 50 + "\r\n" for i in range(5000))]) + "\n")
        f.write(json.dumps([20.0, "o", "après le flood\r\n"]) + "\n")
    got = []
    snaps, _ = terminal.replay(p, interval=5, on_snapshot=got.append)
    assert snaps == []  # mode streaming
    text = "\n".join(s.text for s in got)
    assert terminal.TRUNCATED_NOTE in text
    assert "ligne 00000" in text and "ligne 04000" not in text
    assert "après le flood" in text  # le budget repart au tick suivant


def test_heuristic_dedup_tmux_double_read(tmp_path):
    import json
    p = tmp_path / "dup.cast"
    P = "[x] host /workspace # "
    with open(p, "w") as f:
        f.write(json.dumps({"version": 2, "width": 80, "height": 10, "timestamp": 0}) + "\n")
        for t, d in [(0.1, P + "id"), (1.0, "\r\n"), (1.1, P + "id"), (1.2, "\r\n"), (9.0, P + "id"), (9.5, "\r\n")]:
            f.write(json.dumps([t, "o", d]) + "\n")
    _, detected = terminal.replay(p)
    assert [(d.offset, d.cmd) for d in detected] == [(1.0, "id"), (9.5, "id")]
