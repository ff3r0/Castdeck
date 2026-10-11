import json
from pathlib import Path

from castdeck import asciicast as ac

FIX = Path(__file__).parent / "fixtures"


def test_v2_header_and_scan():
    h = ac.read_header(FIX / "small_v2.cast")
    assert (h.version, h.cols, h.rows) == (2, 80, 24)
    assert h.timestamp == 1791630000
    info = ac.scan(FIX / "small_v2.cast")
    assert info.duration == 15.0
    assert info.n_events == 13
    assert info.markers == [(3.5, "scan fini")]


def test_v3_relative_times_converted():
    path = FIX / "small_v3.asciinema"
    h = ac.read_header(path)
    assert (h.version, h.cols, h.rows) == (3, 100, 30)
    events = list(ac.iter_events(path))
    times = [round(t, 6) for t, _, _ in events]
    assert times == [0.5, 2.0, 2.1, 2.2, 12.2, 12.3, 12.4, 13.0]
    info = ac.scan(path)
    assert round(info.duration, 6) == 13.0
    assert info.markers and round(info.markers[0][0], 6) == 12.4


def test_v3_to_v2_conversion():
    lines = list(ac.to_v2_lines(FIX / "small_v3.asciinema"))
    header = json.loads(lines[0])
    assert header["version"] == 2 and header["width"] == 100 and header["height"] == 30
    assert header["env"] == {"TERM": "xterm-256color"}
    events = [json.loads(l) for l in lines[1:]]
    assert all(e[1] != "x" for e in events)  # "exit" n'existe pas en v2
    assert [e[0] for e in events] == [0.5, 2.0, 2.1, 2.2, 12.2, 12.3, 12.4]
    assert all(l.endswith("\n") for l in lines)


def test_detection_by_header_not_extension(tmp_path):
    p = tmp_path / "session.log"
    p.write_text((FIX / "small_v2.cast").read_text())
    assert ac.sniff(p) is not None
    junk = tmp_path / "fake.cast"
    junk.write_text("pas du json\n[1, \"o\", \"x\"]\n")
    assert ac.sniff(junk) is None
    v1 = tmp_path / "old.cast"
    v1.write_text(json.dumps({"version": 1, "width": 80, "height": 24}) + "\n")
    assert ac.sniff(v1) is None


def test_resize_and_truncated_last_line():
    path = FIX / "resize.cast"
    events = list(ac.iter_events(path))
    assert [c for _, c, _ in events] == ["o", "r", "o", "r", "o"]  # ligne tronquée ignorée
    assert ac.parse_resize("120x40") == (120, 40)
    assert ac.parse_resize("bad") is None


def test_transform_applies_only_to_io():
    lines = list(ac.to_v2_lines(FIX / "small_v2.cast", transform=lambda s: s.upper()))
    events = [json.loads(l) for l in lines[1:]]
    assert any(e[2] == "STARTING NMAP 7.94\r\nPORT   STATE SERVICE\r\n22/TCP OPEN  SSH\r\n" for e in events)
    assert ["m", "scan fini"] == [e for e in events if e[1] == "m"][0][1:]


def test_files_untouched(tmp_path):
    p = tmp_path / "a.cast"
    data = (FIX / "small_v2.cast").read_bytes()
    p.write_bytes(data)
    list(ac.to_v2_lines(p, idle_limit=1))
    ac.scan(p)
    assert p.read_bytes() == data
