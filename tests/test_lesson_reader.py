"""整节读 Ed lesson、Ed 文件的 read_file、老库迁移、Retry-After。"""
import json
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from monash_study_kit import edlib, edquery, lesson_reader as LR, lessons, moodlelib, webnotes


@pytest.fixture
def ed(tmp_path, monkeypatch):
    files, ed_files = tmp_path / "files", tmp_path / "ed-files"
    monkeypatch.setattr(LR, "FILES_DIR", files)
    monkeypatch.setattr(LR, "ED_FILES_DIR", ed_files)
    moodle = moodlelib.db_connect(tmp_path / "moodle.db")
    moodle.executescript(webnotes.SCHEMA)
    (files / "U" / "Course notes (web)").mkdir(parents=True)
    (files / "U" / "Course notes (web)" / "Shell.md").write_text(
        "# 1.2 What Is a Shell?\n\n来源：https://notes.example/w1/shell.html\n\nA shell is a command interface.\n",
        encoding="utf-8")
    moodle.execute("INSERT INTO web_pages (url, course, title, path, status, fetched_at) VALUES "
                   "('https://notes.example/w1/shell.html', 'FIT2109', 'Shell', 'U/Course notes (web)/Shell.md', 'ok', 'now')")
    moodle.commit()
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path)      # lesson_reader 用 db_connect() 找 web_pages

    conn = edlib.db_connect(tmp_path / "ed.db")
    lessons.ensure_lessons_schema(conn)
    conn.execute("INSERT INTO courses (id, code, name, tracked) VALUES (39026, 'FIT2109 S2 2026', 'CSW', 1)")
    conn.execute("INSERT INTO modules VALUES (1, 39026, 'Week 1', NULL)")
    for lid, title in ((7, "W1 Pre-Class: Shell"), (8, "W1 Workshop")):
        conn.execute("INSERT INTO lessons (id, course_id, module_id, idx, title, status) VALUES (?,39026,1,?,?,'unattempted')",
                     (lid, lid, title))
    (ed_files / "39026" / "7").mkdir(parents=True)
    (ed_files / "39026" / "7" / "91_Intro.md").write_text("# W1 Pre-Class: Shell · Intro\n\nWelcome to week 1.\n",
                                                          encoding="utf-8")
    rows = [  # 故意把 id 顺序和页序反过来，确认按 idx 排
        (93, "webpage", "Shell reading", None, "https://notes.example/w1/shell.html#top", None, 1),
        (91, "document", "Intro", None, None, "39026/7/91_Intro.md", 0),
        (92, "webpage", "Video", None, "https://youtu.be/abc", None, 2),
    ]
    for r in rows:
        conn.execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title, file_url, url, local_path, idx)"
                     " VALUES (?,7,39026,?,?,?,?,?,?)", r)
    conn.execute("INSERT INTO quiz_questions VALUES (1, 95, 7, 39026, 'Check', 0, 'multiple-choice', 0, "
                 "'What is a shell?', ?, 'now')", (json.dumps(["A program", "The kernel"]),))
    conn.commit()
    return conn


def test_lesson_in_slide_order_with_web_page_and_quiz(ed):
    d = LR.lesson_markdown(ed, "FIT2109", "pre-class")
    md = d["markdown"]
    assert md.startswith("# W1 Pre-Class: Shell")
    assert md.index("Welcome to week 1") < md.index("A shell is a command interface") < md.index("youtu.be")
    assert "# W1 Pre-Class: Shell · Intro" not in md                 # 存档里的重复标题去掉
    assert "没有抓下来" in md and "**1.** What is a shell?" in md and "B. The kernel" in md
    assert d["readings"] == 2


def test_lesson_lookup(ed):
    assert LR.find_lesson(ed, "FIT2109", "8")["title"] == "W1 Workshop"
    with pytest.raises(edquery.NotFound, match="匹配到好几节"):
        LR.find_lesson(ed, "FIT2109", "W1")
    with pytest.raises(edquery.NotFound, match="有这些"):
        LR.find_lesson(ed, "FIT2109", "W9")


def test_old_db_gets_slide_order_column(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE lesson_files (id INTEGER PRIMARY KEY, lesson_id INTEGER, course_id INTEGER, type TEXT,"
                " title TEXT, file_url TEXT, url TEXT, local_path TEXT)")
    lessons.ensure_lessons_schema(con)
    assert "idx" in {r[1] for r in con.execute("PRAGMA table_info(lesson_files)")}
    lessons.ensure_lessons_schema(con)                                  # 再跑一次不出错


def test_slide_order_recorded_from_api():
    files = lessons.fetch_lesson_files(None, 1, [{"id": 5, "type": "webpage", "url": "https://x", "index": 3},
                                                 {"id": 6, "type": "pdf", "file_url": "https://y"}])
    assert [(f["id"], f["idx"]) for f in files] == [(5, 3), (6, 1)]      # 没给 index 就用位置


def test_read_file_opens_ed_files_but_not_outside(tmp_path, monkeypatch):
    from monash_study_kit import mcp_server as M
    monkeypatch.setattr(M, "ED_FILES_DIR", tmp_path / "ed")
    (tmp_path / "ed" / "1").mkdir(parents=True)
    (tmp_path / "ed" / "1" / "a.md").write_text("hello ed", encoding="utf-8")
    (tmp_path / "secret").write_text("token", encoding="utf-8")

    def read(p):
        r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "read_file", "arguments": {"path": p}}})["result"]
        return r.get("isError"), r["content"][0]["text"]
    assert read("ed:1/a.md") == (None, "hello ed")
    assert read("ed:../secret")[0]


def test_retry_after_is_honoured_and_capped():
    assert edlib.retry_delay(None, 1) == 3.0
    assert edlib.retry_delay("10", 0) == 10.0                            # 比退避长就听 Ed 的
    assert edlib.retry_delay("1", 2) == 6.0                              # 比退避短就按退避
    assert edlib.retry_delay("9999", 0) == edlib.RETRY_AFTER_CAP
    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 25 <= edlib.retry_delay(later, 0) <= 30
    assert edlib.retry_delay("garbage", 0) == 1.5
