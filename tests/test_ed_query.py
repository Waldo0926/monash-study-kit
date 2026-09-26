import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monash_study_kit import edlib
from monash_study_kit import edquery as query


def iso(**ago) -> str:
    return (datetime.now(timezone.utc) - timedelta(**ago)).isoformat(timespec="seconds")


def seed(conn):
    conn.executescript(edlib.SCHEMA)
    conn.executemany("INSERT INTO courses (id, code, name, tracked) VALUES (?,?,?,?)", [
        (36340, "FIT2102", "Programming paradigms", 1),
        (39026, "FIT2109 S2 2026 Malaysia ", "Computer science workshop", 1),
        (11111, "FIT1000", "Old unit", 0),
    ])
    # Ed 的时间带 +10:00；3 小时前发的帖，按字符串比会被 --since 2h 误判
    local = datetime.now(timezone(timedelta(hours=10)))
    rows = [
        (1, 36340, 42, "question", "Quiz 3 deadline", "Quizzes", "Alice", 0, 0, 2,
         (local - timedelta(hours=3)).isoformat(), "When is quiz 3 due?", iso(hours=3)),
        (2, 36340, 43, "announcement", "A1 marks are up", "Assignments", "Staff", 1, 1, 0,
         (local - timedelta(days=5)).isoformat(), "See [marks](https://x/m)", iso(days=5)),
        (3, 39026, 42, "question", "Interview allocation", "Assignments", "Bob", 0, 0, 1,
         (local - timedelta(days=1)).isoformat(), "Where is the sheet?", iso(days=1)),
    ]
    for r in rows:
        conn.execute("""INSERT INTO threads (id, course_id, number, type, title, category, author,
            is_pinned, is_staff_answered, reply_count, created_at, updated_at, body, url, first_seen,
            is_private, is_answered)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
                     (*r[:10], r[10], r[10], r[11], f"https://edstem.org/au/courses/{r[1]}/discussion/{r[0]}", r[12]))
    conn.executemany("""INSERT INTO replies (id, thread_id, parent_id, kind, depth, author, is_staff,
        is_endorsed, vote_count, created_at, text, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (10, 1, None, "answer", 0, "Tutor", 1, 1, 0, iso(hours=2), "Friday 11:55pm, on Moodle", iso(hours=2)),
        (11, 1, 10, "comment", 1, "Alice", 0, 0, 0, iso(hours=1), "thanks!", iso(minutes=5)),
        (12, 3, None, "comment", 0, "Tutor", 1, 0, 0, iso(days=1), "Pinned post has the allocation sheet", iso(days=1)),
    ])
    conn.execute("INSERT INTO attachments VALUES (1, 10, 'file', 'rubric.pdf', 'https://s/r')")
    conn.execute("INSERT INTO runs (started_at, finished_at) VALUES (?, ?)", (iso(minutes=10), iso(minutes=9)))
    conn.commit()


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = edlib.DATA_DIR
        edlib.DATA_DIR = Path(self.tmp.name)
        self.conn = edlib.db_connect(Path(self.tmp.name) / "ed.db")
        seed(self.conn)

    def tearDown(self):
        edlib.DATA_DIR = self.old_data
        self.conn.close()
        self.tmp.cleanup()

    def test_course_by_code_word_id_or_name(self):
        self.assertEqual(query.resolve_course(self.conn, "FIT2109")["id"], 39026)
        self.assertEqual(query.resolve_course(self.conn, "fit2102")["id"], 36340)
        self.assertEqual(query.resolve_course(self.conn, "36340")["id"], 36340)
        self.assertEqual(query.resolve_course(self.conn, "paradigms")["id"], 36340)
        with self.assertRaises(query.NotFound):
            query.resolve_course(self.conn, "FIT9999")

    def test_thread_refs(self):
        self.assertEqual(query.resolve_thread(self.conn, "FIT2109#42"), 3)
        self.assertEqual(query.resolve_thread(self.conn, "https://edstem.org/au/courses/36340/discussion/2"), 2)
        self.assertEqual(query.resolve_thread(self.conn, "1"), 1)
        with self.assertRaises(query.NotFound):     # #42 两门课都有
            query.resolve_thread(self.conn, "#42")

    def test_since_compares_real_instants_not_strings(self):
        refs = [t["ref"] for t in query.list_threads(self.conn, since="4h")]
        self.assertEqual(refs, ["FIT2102#42"])
        self.assertEqual([t["ref"] for t in query.list_threads(self.conn, since="2h")], [])

    def test_filters_and_untracked_courses_hidden(self):
        self.assertEqual([t["id"] for t in query.list_threads(self.conn)], [1, 3, 2])
        self.assertEqual([t["id"] for t in query.list_threads(self.conn, unanswered=True)], [1, 3])
        self.assertEqual([t["id"] for t in query.list_threads(self.conn, "FIT2102", category="assign")], [2])
        self.assertEqual(query.list_threads(self.conn, "FIT2102", type_="announcement")[0]["title"], "A1 marks are up")

    def test_search_needs_every_word_and_looks_in_replies(self):
        hits = query.search(self.conn, "friday moodle")
        self.assertEqual([h["id"] for h in hits], [1])
        self.assertEqual(hits[0]["match_in"], "reply")
        self.assertEqual(query.search(self.conn, "quiz nothing-like-this"), [])
        self.assertEqual([h["id"] for h in query.search(self.conn, "allocation", course="FIT2109")], [3])

    def test_thread_nests_replies_and_attachments(self):
        t = query.get_thread(self.conn, "FIT2102#42")
        self.assertEqual([r["id"] for r in t["replies"]], [10, 11])
        self.assertEqual(t["replies"][0]["attachments"], [{"kind": "file", "name": "rubric.pdf", "url": "https://s/r"}])
        md = query.thread_markdown(t)
        self.assertIn("# Quiz 3 deadline", md)
        self.assertIn("**✅ Tutor**", md)
        self.assertIn("  thanks!", md)

    def test_new_activity_advances_cursor(self):
        first = query.new_activity(self.conn, cursor="t")          # 第一次默认 3 天
        self.assertEqual([t["id"] for t in first["new_threads"]], [1, 3])
        self.assertEqual(first["threads_with_new_replies"], [])    # 帖 1 本身就是新帖，不重复列
        again = query.new_activity(self.conn, cursor="t")
        self.assertEqual(again["new_threads"], [])
        peek = query.new_activity(self.conn, since="30m", cursor="t", advance=True)
        self.assertEqual([r["text"] for t in peek["threads_with_new_replies"] for r in t["new_replies"]], ["thanks!"])
        self.assertEqual(query.read_cursor("t"), again["until"])   # 给了 since 不动游标

    def test_since_parsing(self):
        self.assertTrue(query.parse_since("2026-09-01").startswith("2026-08-3") or
                        query.parse_since("2026-09-01").startswith("2026-09-01"))
        with self.assertRaises(ValueError):
            query.parse_since("yesterday")


class CliTests(unittest.TestCase):
    """管道里默认输出 JSON；找不到东西退 4。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        conn = edlib.db_connect(Path(self.tmp.name) / "data" / "ed.db")
        seed(conn)
        conn.close()
        self.env = {**os.environ, "MONASH_KIT_HOME": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "monash_study_kit", "ed", *args], env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_piped_output_is_json(self):
        r = self.run_cli("threads", "FIT2102")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([t["ref"] for t in json.loads(r.stdout)], ["FIT2102#42", "FIT2102#43"])

    def test_text_flag_and_search_without_course(self):
        r = self.run_cli("search", "allocation", "--text")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FIT2109#42", r.stdout)

    def test_show_markdown_and_missing_thread(self):
        self.assertIn("rubric.pdf", self.run_cli("show", "1", "--json").stdout)
        r = self.run_cli("show", "FIT2102#999")
        self.assertEqual(r.returncode, 4)
        self.assertIn("没有帖子", r.stderr)


class ThreadStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = edlib.db_connect(Path(self.tmp.name) / "ed.db")
        seed(self.conn)
        # 列表项的样子照 Ed 实际返回：is_watched 没关注时是 null
        edlib.save_thread_state(self.conn, [
            {"id": 1, "user_id": 1001, "is_starred": False, "is_watched": True, "is_seen": True,
             "new_reply_count": 1, "accepted_id": 10},
            {"id": 2, "user_id": 5, "is_starred": True, "is_watched": None, "is_seen": True,
             "new_reply_count": 0, "accepted_id": None},
            {"id": 3, "user_id": 6, "is_starred": False, "is_watched": None, "is_seen": False,
             "new_reply_count": None, "accepted_id": None},
        ], me=1001)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def refs(self, **kw):
        return [t["ref"] for t in query.list_threads(self.conn, **kw)]

    def test_state_filters(self):
        self.assertEqual(self.refs(state=("starred",)), ["FIT2102#43"])
        self.assertEqual(self.refs(state=("mine",)), ["FIT2102#42"])
        self.assertEqual(self.refs(state=("watching",)), ["FIT2102#42"])
        self.assertEqual(self.refs(state=("unseen",)), ["FIT2109#42"])
        self.assertEqual(self.refs(state=("mine", "starred")), [])
        row = query.list_threads(self.conn, state=("mine",))[0]
        self.assertTrue(row["is_mine"] and row["is_watched"] and not row["is_starred"])
        self.assertEqual(row["new_reply_count"], 1)

    def test_following_returns_only_the_unread_replies(self):
        rows = query.following(self.conn)
        self.assertEqual([t["ref"] for t in rows], ["FIT2102#42"])
        self.assertEqual([r["text"] for r in rows[0]["new_replies"]], ["thanks!"])   # 最新的 1 条

    def test_accepted_answer_is_marked(self):
        md = query.thread_markdown(query.get_thread(self.conn, "FIT2102#42"))
        self.assertIn("✔ 已采纳 ✅ Tutor", md)
        self.assertIn("我发的", md)

    def test_threads_without_state_still_list(self):
        self.conn.execute("DELETE FROM thread_state")
        self.assertEqual(len(query.list_threads(self.conn)), 3)
