"""同步时的各种“半路出事”：网络抖一下、文件下到一半、Ed 上删了东西。"""
import urllib.error
import zipfile

from monash_study_kit import content_index as CI
from monash_study_kit import edlib, edsync, textextract
from monash_study_kit import lessons as L
from monash_study_kit.browser_login import _domain_matches


class FlakyEd:
    """lesson 列表能拿到，单节 lesson 的详情 500。"""

    def get(self, path, params=None):
        if path.endswith("/lessons"):
            return {"modules": [], "lessons": [{"id": 7, "title": "W3 Pre-Class", "index": 0}]}
        raise urllib.error.HTTPError(path, 500, "boom", None, None)


def _lesson_db(tmp_path):
    conn = edlib.db_connect(tmp_path / "ed.db")
    L.ensure_lessons_schema(conn)
    conn.execute("INSERT INTO lessons (id, course_id, title) VALUES (7, 1, 'W3 Pre-Class'), (8, 1, 'Removed')")
    conn.execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title) VALUES (70, 7, 1, 'pdf', 'Slides')")
    conn.execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title) VALUES (80, 8, 1, 'pdf', 'Old')")
    conn.execute("INSERT INTO quiz_questions (id, slide_id, lesson_id, course_id, question) VALUES (1, 71, 7, 1, 'Q')")
    conn.commit()
    return conn


def test_failed_lesson_fetch_keeps_old_files_and_quiz(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "FILES_DIR", tmp_path / "ed-files")
    (tmp_path / "ed-files" / "1" / "8").mkdir(parents=True)
    conn = _lesson_db(tmp_path)
    L.sync_lessons(conn, FlakyEd(), 1, log=lambda m: None)
    # 第 7 节这次没取到详情：上次的课件和测验题都还在
    assert [r[0] for r in conn.execute("SELECT id FROM lesson_files WHERE lesson_id=7")] == [70]
    assert conn.execute("SELECT COUNT(*) FROM quiz_questions WHERE lesson_id=7").fetchone()[0] == 1
    # 第 8 节在 Ed 上已经没了：连记录带下载的文件一起删掉
    assert conn.execute("SELECT COUNT(*) FROM lessons WHERE id=8").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lesson_files WHERE lesson_id=8").fetchone()[0] == 0
    assert not (tmp_path / "ed-files" / "1" / "8").exists()
    conn.close()


def test_half_downloaded_pdf_is_not_kept(tmp_path, monkeypatch):
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise TimeoutError("network dropped")

    monkeypatch.setattr(L.urllib.request, "urlopen", lambda *a, **k: Broken())
    dest = tmp_path / "1" / "7" / "slides.pdf"
    assert L.download_pdf("https://static.edusercontent.com/x.pdf", dest, log=lambda m: None) is False
    assert not dest.exists() and not dest.with_name("slides.pdf.part").exists()


def test_replies_deleted_on_ed_are_removed_locally(tmp_path):
    conn = edlib.db_connect(tmp_path / "ed.db")
    t = {"id": 5, "number": 42, "title": "Q", "type": "question", "user_id": 1}

    def full(*answers):
        return {"thread": {"content": "<document><paragraph>hi</paragraph></document>",
                           "answers": [{"id": a, "user_id": 2, "content": "<document><paragraph>ans</paragraph></document>"}
                                       for a in answers]}, "users": []}

    edsync.store_thread(conn, 1, t, full(100, 101), "s1")
    assert {r[0] for r in conn.execute("SELECT id FROM replies")} == {100, 101}
    _, new = edsync.store_thread(conn, 1, t, full(101), "s2", prev={"content_hash": "s1"})
    assert new == 0 and {r[0] for r in conn.execute("SELECT id FROM replies")} == {101}
    conn.close()


def test_office_text_joins_runs_and_unescapes(tmp_path):
    pptx = tmp_path / "w5.pptx"
    with zipfile.ZipFile(pptx, "w") as z:
        z.writestr("ppt/slides/slide1.xml", "<p:sld><a:p><a:r><a:t>Mon</a:t></a:r><a:r><a:t>ads</a:t></a:r>"
                                            "<a:br/><a:r><a:t>x &lt; y &amp;&amp; z</a:t></a:r></a:p></p:sld>")
    assert textextract.office_parts(pptx) == [("slide 1", "Monads x < y && z")]
    docx = tmp_path / "spec.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:r><w:t>Q&amp;A</w:t></w:r></w:p><w:p><w:r><w:t>end</w:t></w:r></w:p>")
    assert textextract.office_parts(docx)[0][1].split() == ["Q&A", "end"]


def test_one_bad_file_does_not_stop_indexing(tmp_path, monkeypatch):
    files = tmp_path / "files" / "FIT2102 x (S2 2026)"
    files.mkdir(parents=True)
    (files / "good.md").write_text("functor laws", encoding="utf-8")
    (files / "bad.docx").write_bytes(b"not really a docx")
    monkeypatch.setattr(CI, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    real = CI.extract
    monkeypatch.setattr(CI, "extract", lambda p: (_ for _ in ()).throw(RuntimeError("encrypted"))
                        if p.suffix == ".docx" else real(p))
    import sqlite3
    con = sqlite3.connect(":memory:")
    assert CI.index(con)["indexed"] == 1
    assert CI.search(con, "functor")[0]["title"].endswith("good.md")


def test_cookie_domain_must_match_on_a_dot_boundary():
    assert _domain_matches(".monash.edu") and _domain_matches("learning.monash.edu")
    assert not _domain_matches("ash.edu") and not _domain_matches("") and not _domain_matches("evil.com")
