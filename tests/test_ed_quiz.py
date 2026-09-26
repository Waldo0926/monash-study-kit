import tempfile
from pathlib import Path

from monash_study_kit import edlib
from monash_study_kit import lessons as L
from monash_study_kit import edquery as query

Q = '<document version="2.0"><paragraph>{}</paragraph></document>'


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        return {"questions": [
            {"id": 2, "index": 1, "data": {"type": "multiple-choice", "multiple_selection": True,
                                            "content": Q.format("Pick <b>two</b>"), "answers": [Q.format("x"), Q.format("y")]}},
            {"id": 1, "index": 0, "data": {"type": "multiple-choice", "multiple_selection": False,
                                            "content": Q.format("What is a buffer?"), "answers": [Q.format("RAM"), Q.format("Disk")]}},
        ]}


def test_quiz_sync_store_and_render():
    with tempfile.TemporaryDirectory() as d:
        conn = edlib.db_connect(Path(d) / "ed.db")
        L.ensure_lessons_schema(conn)
        conn.execute("INSERT INTO courses (id, code, name, tracked) VALUES (39026, 'FIT2109', 'x', 1)")
        conn.execute("INSERT INTO modules VALUES (1, 39026, 'Week 3: Editors', NULL)")
        conn.execute("INSERT INTO lessons (id, course_id, module_id, idx, title, status) "
                     "VALUES (7, 39026, 1, 0, 'W3 Pre-Class', 'unattempted')")
        client = FakeClient()
        slides = [{"id": 70, "type": "quiz", "title": "Check your understanding"}, {"id": 71, "type": "pdf"}]
        assert L.sync_quizzes(conn, client, 39026, 7, slides) == 2
        assert L.sync_quizzes(conn, client, 39026, 7, slides) == 0      # 存过的不重抓
        assert client.calls == ["/lessons/slides/70/questions"]

        rows = query.quizzes(conn, "FIT2109", status="unattempted")
        qs = rows[0]["quizzes"][0]["questions"]
        assert [q["n"] for q in qs] == [1, 2] and qs[0]["options"] == ["RAM", "Disk"] and qs[1]["multiple"]
        md = query.quiz_markdown(rows)
        assert "**1.** What is a buffer?" in md and "   B. Disk" in md and "（多选）" in md
        assert query.quizzes(conn, "FIT2109", status="completed") == []

        L.sync_quizzes(conn, client, 39026, 7, [])                      # quiz 页被删了
        assert conn.execute("SELECT COUNT(*) FROM quiz_questions").fetchone()[0] == 0
