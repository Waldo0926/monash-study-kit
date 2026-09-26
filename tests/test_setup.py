"""接进 Claude、选课、同步锁、设置、录像默认不下载、没登录时退回 iCal。"""
import json
import time

from monash_study_kit import claude_setup, features, jobs, paths


def test_desktop_config_keeps_other_servers_and_backs_up(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "globalShortcut": "Alt+Space"}), encoding="utf-8")
    backup = claude_setup.write_desktop_config(cfg, ["/bin/monash", "mcp"])
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["monash"] == {"command": "/bin/monash", "args": ["mcp"]}
    assert data["mcpServers"]["other"] == {"command": "x"} and data["globalShortcut"] == "Alt+Space"
    assert json.loads(backup.read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "x"}}
    assert claude_setup.remove_from_desktop_config(cfg)
    assert "monash" not in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]


def test_desktop_config_created_when_missing(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    assert claude_setup.write_desktop_config(cfg, ["monash", "mcp"]) is None
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["monash"]["command"] == "monash"


def test_current_courses_picks_this_semester():
    now = time.time()
    courses = [
        {"id": 1, "fullname": "FIT2102 Programming paradigms - S2 2026", "startdate": now - 60 * 86400, "enddate": now + 60 * 86400},
        {"id": 2, "fullname": "FIT1045 Intro - S1 2025", "startdate": now - 500 * 86400, "enddate": now - 300 * 86400},
        {"id": 3, "fullname": "Monash Malaysia Student Hub", "startdate": now - 30 * 86400, "enddate": 0},
        {"id": 4, "fullname": "FIT2109 Workshop - S2 2026", "startdate": now - 60 * 86400, "enddate": 0},
    ]
    assert [c["id"] for c in jobs.current_courses(courses, now)] == [1, 4]


def test_sync_lock_is_exclusive_and_stale_lock_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "HOME", tmp_path)
    monkeypatch.setattr(jobs, "LOCK_FILE", tmp_path / "sync.lock")
    assert jobs._lock() and not jobs._lock()
    jobs._unlock()
    assert jobs._lock()
    old = time.time() - 3 * 3600
    import os
    os.utime(tmp_path / "sync.lock", (old, old))
    assert jobs._lock()                                # 上次崩了留下的锁
    jobs._unlock()


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    monkeypatch.setattr(paths, "SETTINGS_FILE", tmp_path / "settings.json")
    assert paths.load_settings()["download_videos"] is False
    paths.save_settings({"download_videos": True})
    assert paths.load_settings() == {**paths.DEFAULTS, "download_videos": True}


def test_videos_become_links_unless_enabled(tmp_path, monkeypatch):
    from monash_study_kit import moodlelib, syncer
    monkeypatch.setattr(syncer, "FILES_DIR", tmp_path)
    monkeypatch.setattr(syncer, "load_settings", lambda: {"download_videos": False})
    con = moodlelib.db_connect(tmp_path / "m.db")
    s = syncer.CourseSync(client=None, con=con, course={"id": 7, "fullname": "FIT2102 X - S2 2026"})
    s.fetch_file("https://learning.monash.edu/pluginfile.php/1/mod_resource/content/3/Week%201.mp4",
                 "Week 01", {"id": 5, "name": "Lecture recording"}, "Week 1")
    assert s.stats.downloaded == 0 and s.stats.links == 1
    assert con.execute("SELECT title FROM links").fetchone()["title"] == "🎬 Lecture recording"


def test_due_without_login_uses_ical(monkeypatch):
    now = int(time.time())
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now + 3 * 86400))
    ical = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:123@learning.monash.edu\r\nSUMMARY:A2 is due\r\n"
            f"DTSTART:{stamp}\r\nCATEGORIES:FIT2102_S2_2026\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    monkeypatch.setattr(features, "load_calendar_url", lambda: "https://x/export_execute.php?authtoken=1")
    monkeypatch.setattr(features, "fetch_ical", lambda url: ical)
    items = features.due(None, 14)
    assert [(i["name"], i["course"], i["source"]) for i in items] == [("A2", "FIT2102", "ical")]
    other = {"id": None, "shortname": "FIT2109", "fullname": "FIT2109"}
    assert features.due(None, 14, other) == []
