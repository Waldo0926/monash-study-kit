import sqlite3
from datetime import datetime, timedelta, timezone

from monash_study_kit import todo
from monash_study_kit.moodlelib import MoodleAuthError


def ed_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE courses (id INTEGER PRIMARY KEY, code TEXT, tracked INTEGER);
        CREATE TABLE modules (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE lessons (id INTEGER PRIMARY KEY, course_id INTEGER, module_id INTEGER, title TEXT,
                              status TEXT, is_hidden INTEGER);
        CREATE TABLE threads (id INTEGER PRIMARY KEY, course_id INTEGER, number INTEGER, type TEXT, title TEXT,
                              created_at TEXT, updated_at TEXT, url TEXT);
        CREATE TABLE thread_state (thread_id INTEGER PRIMARY KEY, new_reply_count INTEGER, is_mine INTEGER,
                                   is_watched INTEGER);
    """)
    con.execute("INSERT INTO courses VALUES (39026, 'FIT2109 S2 2026 Malaysia ', 1), (1, 'FIT9999', 0)")
    con.execute("INSERT INTO modules VALUES (1, 'Week 3: Editors'), (2, 'Week 4: Git'), (3, 'Week 9: Perf')")
    con.executemany("INSERT INTO lessons VALUES (?,?,?,?,?,0)", [
        (1, 39026, 1, "W3 Pre-Class", "unattempted"),
        (2, 39026, 1, "W3 Workshop", "completed"),
        (3, 39026, 2, "W4 Workshop", "attempted"),       # 进度到第 4 周 → 列到第 5 周
        (4, 39026, 3, "W9 Pre-Class", "unattempted"),    # 太远，不列
        (5, 39026, None, "Getting Started", "unattempted"),   # 没有周次，不列
        (6, 1, 1, "W1 Other unit", "unattempted"),       # 没跟踪的课
    ])
    now = datetime.now(timezone(timedelta(hours=10)))
    con.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", [
        (10, 39026, 402, "announcement", "Interview slots", (now - timedelta(days=2)).isoformat(), None, "u1"),
        (11, 39026, 12, "announcement", "Old news", (now - timedelta(days=30)).isoformat(), None, "u2"),
        (12, 39026, 99, "question", "My question", (now - timedelta(days=1)).isoformat(), now.isoformat(), "u3"),
    ])
    con.execute("INSERT INTO thread_state VALUES (12, 2, 1, 0)")
    con.commit()
    con.close()


class DeadClient:
    def call(self, *a, **k):
        raise MoodleAuthError("expired")

    def html(self, *a, **k):
        raise MoodleAuthError("expired")


def test_todo_ed_parts_and_dead_session(tmp_path, monkeypatch):
    ed_db(tmp_path / "ed.db")
    monkeypatch.setattr(todo, "ED_DB", tmp_path / "ed.db")
    monkeypatch.setattr(todo.F, "load_calendar_url", lambda: None)
    out = todo.build(DeadClient(), [{"id": 42804, "fullname": "FIT2109 x"}], days=7)

    assert [(l["week"], l["title"], l["status"]) for l in out["ed_lessons"]] == [
        (3, "W3 Pre-Class", "unattempted"), (4, "W4 Workshop", "attempted")]
    assert [a["ref"] for a in out["announcements"]] == ["FIT2109#402"]
    assert out["ed_unread_replies"] == [{"ref": "FIT2109#99", "title": "My question", "new": 2,
                                         "mine_or_watched": True, "url": "u3"}]
    assert out["due"] == [] and out["possibly_missing"] == []
    assert any("截止日期缺失" in n for n in out["notes"])
    assert any("作业汇总" in n for n in out["notes"])


def test_todo_without_ed(tmp_path, monkeypatch):
    monkeypatch.setattr(todo, "ED_DB", tmp_path / "nope" / "ed.db")
    monkeypatch.setattr(todo.F, "load_calendar_url", lambda: None)
    out = todo.build(DeadClient(), [], days=7)
    assert out["ed_lessons"] == [] and any("还没同步过 Ed" in n for n in out["notes"])


class OfflineClient:
    def call(self, *a, **k):
        from monash_study_kit.moodlelib import MoodleError
        raise MoodleError("GET /my/courses.php -> 503")

    def html(self, *a, **k):
        import urllib.error
        raise urllib.error.URLError("no network")


def test_todo_keeps_ed_parts_when_moodle_is_unreachable(tmp_path, monkeypatch):
    ed_db(tmp_path / "ed.db")
    monkeypatch.setattr(todo, "ED_DB", tmp_path / "ed.db")
    out = todo.build(OfflineClient(), [{"id": 42804, "fullname": "FIT2109 x"}], days=7)
    assert out["due"] == [] and out["possibly_missing"] == []
    assert [a["ref"] for a in out["announcements"]] == ["FIT2109#402"]      # Ed 部分照常
    assert sum("连不上 Moodle" in n for n in out["notes"]) == 2
