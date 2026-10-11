import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from castdeck.index import Config, Index, make_snippet
from castdeck.server import make_server

FIX = Path(__file__).parent / "fixtures"
TS = 1791630000  # 2026-10-10 11:00:00 UTC


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    shutil.copy(FIX / "small_v2.cast", d / "2026-10-10_session1.asciinema")
    shutil.copy(FIX / "small_v3.asciinema", d / "2026-10-10_session2.cast")
    (d / "notes.txt").write_text("pas un cast")
    # heure locale = UTC pour des tests déterministes
    (d / "commands.log").write_text(
        "2026-10-10 11:00:02 [cli] nmap -sV 10.10.10.5\n"
        "2026-10-10 11:00:14 [%2] cat /etc/passwd\n"
        "2026-10-10 15:00:00 [cli] hors session\n")
    (d / "panes_%2.log").write_text("ligne pane\nsecretsdump résultat nmap-like\n")
    return d


def make_index(logs, **kw):
    idx = Index(Config(root=logs, tz="UTC", **kw))
    idx.build()
    return idx


def test_build_sessions_and_commands(logs):
    idx = make_index(logs)
    conn = idx.connect()
    sessions = idx.sessions(conn)
    assert [s["file"] for s in sessions] == ["2026-10-10_session1.asciinema", "2026-10-10_session2.cast"]
    s1, s2 = sessions
    assert s1["start_iso"] == "2026-10-10T11:00:00+00:00"
    assert s1["n_commands"] == 2 and s1["markers"] == [[3.5, "scan fini"]]
    cmds = idx.commands(conn, s1["id"])
    assert [(c["offset"], c["ctx"], c["cmd"], c["source"]) for c in cmds] == [
        (2.0, "cli", "nmap -sV 10.10.10.5", "commands.log"),
        (14.0, "%2", "cat /etc/passwd", "commands.log")]
    # session 2 : pas couverte par commands.log -> heuristique après rejeu
    assert idx.commands(conn, s2["id"]) == []
    idx.start_background()
    idx.wait(10)
    assert [c["cmd"] for c in idx.commands(conn, s2["id"])] == ["whoami", "id"]
    assert idx.commands(conn, s2["id"])[0]["source"] == "heuristic"
    assert idx.get_status()["phase"] == "ready"


def test_search_commands_outputs_panes(logs):
    idx = make_index(logs)
    idx.start_background()
    idx.wait(10)
    conn = idx.connect()
    res = idx.search(conn, "nmap", kind="cmd")
    assert len(res) == 1 and res[0]["offset"] == 2.0
    assert res[0]["snippet"] == "<mark>nmap</mark> -sV 10.10.10.5"
    out = idx.search(conn, "open  ssh", kind="output")
    assert any(r["kind"] == "output" and "22/tcp <mark>open</mark>  <mark>ssh</mark>" in r["snippet"] for r in out)
    panes = idx.search(conn, "secretsdump")
    assert panes[0]["kind"] == "pane" and panes[0]["offset"] is None and panes[0]["pane"] == "panes_%2.log"
    # termes courts -> repli LIKE ; filtre de session et de plage horaire
    s1 = idx.sessions(conn)[0]["id"]
    assert all(r["session_id"] == s1 for r in idx.search(conn, "id", session=s1))
    assert idx.search(conn, "whoami", kind="cmd", t_to=TS + 100) == []
    assert len(idx.search(conn, "whoami", kind="cmd", t_from=TS + 3000)) == 1
    # échappement HTML des snippets
    assert make_snippet("<b>nmap</b>", ["nmap"], whole=True) == "&lt;b&gt;<mark>nmap</mark>&lt;/b&gt;"


def test_cache_invalidation(logs):
    idx = make_index(logs)
    idx.start_background(); idx.wait(10)
    conn = idx.connect()
    assert all(s["outputs_indexed"] for s in idx.sessions(conn))
    idx2 = make_index(logs)  # rien n'a changé -> rien à refaire
    assert idx2.get_status()["phase"] == "ready"
    p = logs / "2026-10-10_session2.cast"
    p.write_text(p.read_text() + json.dumps([1.0, "o", "nouveau\r\n"]) + "\n")
    idx3 = make_index(logs)
    flags = {s["file"]: s["outputs_indexed"] for s in idx3.sessions(conn)}
    assert flags == {"2026-10-10_session1.asciinema": True, "2026-10-10_session2.cast": False}
    (logs / "2026-10-10_session1.asciinema").unlink()
    assert len(make_index(logs).sessions(conn)) == 1
    # la commande reste trouvable, mais hors session (plus de position temporelle)
    assert [r["session_id"] for r in idx3.search(conn, "nmap", kind="cmd")] == [None]


def test_redact_index(logs):
    (logs / "commands.log").write_text("2026-10-10 11:00:02 [cli] nxc smb dc -u a -p 'Passw0rd!'\n")
    idx = make_index(logs, redact=True)
    conn = idx.connect()
    s1 = idx.sessions(conn)[0]["id"]
    assert idx.commands(conn, s1)[0]["cmd"] == "nxc smb dc -u a -p ***********"
    assert idx.search(conn, "Passw0rd") == []


@pytest.fixture
def server(logs):
    idx = make_index(logs)
    httpd = make_server(idx, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    idx.start_background(); idx.wait(10)
    yield f"http://127.0.0.1:{httpd.server_address[1]}", idx, logs
    httpd.shutdown()


def get(url, **headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return r.status, dict(r.headers), body


def test_api(server):
    base, idx, logs = server
    _, h, body = get(base + "/api/sessions")
    sessions = json.loads(body)
    assert "default-src 'self'" in h["Content-Security-Policy"]
    sid = sessions[0]["id"]
    assert set(sessions[0]) >= {"id", "file", "start_iso", "duration", "cols", "rows", "n_commands"}
    cmds = json.loads(get(f"{base}/api/sessions/{sid}/commands")[2])
    assert cmds[0] == {"offset": 2.0, "abs_time": TS + 2.0, "ctx": "cli",
                       "cmd": "nmap -sV 10.10.10.5", "source": "commands.log"}
    cast = get(f"{base}/api/sessions/{sessions[1]['id']}/cast")[2].decode().splitlines()
    assert json.loads(cast[0])["version"] == 2  # v3 converti
    assert "idle_time_limit" not in json.loads(cast[0])
    compressed = get(f"{base}/api/sessions/{sid}/cast?idle=1")[2].decode().splitlines()
    assert json.loads(compressed[-1])[0] < json.loads(get(f"{base}/api/sessions/{sid}/cast")[2].decode().splitlines()[-1])[0]
    tm = json.loads(get(f"{base}/api/sessions/{sid}/timemap?idle=1")[2])
    assert tm["limit"] == 1.0 and tm["knots"][0] == [0.0, 0.0]
    res = json.loads(get(f"{base}/api/search?q=nmap&kind=cmd")[2])
    assert res[0]["session_id"] == sid and res[0]["offset"] == 2.0
    screen = json.loads(get(f"{base}/api/sessions/{sid}/screen?t=4")[2])
    assert "22/tcp open" in screen["text"]
    assert json.loads(get(base + "/api/status")[2])["phase"] == "ready"


def test_bookmarks_roundtrip(server):
    base, idx, logs = server
    sid = json.loads(get(base + "/api/sessions")[2])[0]["id"]
    before = (logs / "2026-10-10_session1.asciinema").read_bytes()
    req = urllib.request.Request(f"{base}/api/sessions/{sid}/bookmarks", method="PUT",
                                 data=json.dumps([{"offset": 9, "label": "creds"}, {"offset": 1}]).encode())
    with urllib.request.urlopen(req) as r:
        saved = json.loads(r.read())
    assert [b["offset"] for b in saved] == [1.0, 9.0]
    assert json.loads(get(f"{base}/api/sessions/{sid}/bookmarks")[2])[1]["label"] == "creds"
    stored = json.loads((idx.data_dir / "bookmarks.json").read_text())
    assert list(stored) == ["2026-10-10_session1.asciinema"]
    assert (logs / "2026-10-10_session1.asciinema").read_bytes() == before
    bad = urllib.request.Request(f"{base}/api/sessions/{sid}/bookmarks", method="PUT",
                                 data=b'[{"offset": "x"}]')
    with pytest.raises(HTTPError) as e:
        urllib.request.urlopen(bad)
    assert e.value.code == 400


def test_host_header_protection(server):
    base, _, _ = server
    with pytest.raises(HTTPError) as e:
        get(base + "/api/sessions", Host="evil.example:80")
    assert e.value.code == 403
    req = urllib.request.Request(base + "/api/sessions/000000000000/bookmarks", method="PUT",
                                 data=b"[]", headers={"Origin": "http://evil.example"})
    with pytest.raises(HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_logs_never_modified(logs):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime) for p in logs.iterdir() if p.is_file()}
    idx = make_index(logs)
    idx.start_background(); idx.wait(10)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime) for p in logs.iterdir() if p.is_file()}
    assert before == after
