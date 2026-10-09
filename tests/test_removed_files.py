"""Moodle 上下架或换名重传的文件：标成已移除，列表和搜索里不再出现，放回来就恢复。"""
import sqlite3

from monash_study_kit import content_index as CI
from monash_study_kit import mcp_server as M
from monash_study_kit import moodlelib, syncer
from monash_study_kit.moodlelib import db_connect, removed_paths


def _add(con, source, path, cmid=1):
    con.execute("INSERT INTO files (course_id, source, cmid, path, size, synced_at) VALUES (1, ?, ?, ?, 10, ?)",
                (source, cmid, path, moodlelib.now_iso()))


def _sync(con):
    return syncer.CourseSync(None, con, {"id": 1, "fullname": "FIT2102 Paradigms - S2 2026"})


def test_files_gone_from_moodle_are_marked_then_restored(tmp_path):
    con = db_connect(tmp_path / "t.db")
    _add(con, "https://x/a.zip", "C/a.zip")
    _add(con, "https://x/old.zip", "C/old.zip")
    _add(con, "web:https://notes.github.io/", "C/Course notes (web)/n.md", cmid=None)   # 讲义网页不归同步器管

    s = _sync(con)
    s.seen_sources = {"https://x/a.zip"}
    s.mark_removed()
    assert removed_paths(con) == {"C/old.zip"} and s.stats.removed == 1

    s = _sync(con)                                                     # 老师又放回来了
    s.seen_sources = {"https://x/a.zip", "https://x/old.zip"}
    s.mark_removed()
    assert removed_paths(con) == set()
    con.close()


def test_nothing_marked_when_some_activity_failed(tmp_path):
    con = db_connect(tmp_path / "t.db")
    _add(con, "https://x/a.zip", "C/a.zip")
    s = _sync(con)
    s.stats.errors.append("Week 3 slides: 500")
    s.mark_removed()
    assert removed_paths(con) == set()
    con.close()


def test_new_upload_takes_the_name_of_a_removed_file(tmp_path):
    con = db_connect(tmp_path / "t.db")
    _add(con, "https://learning.monash.edu/pluginfile.php/1/mod_resource/content/3/spec.pdf", "C/W1/spec.pdf")
    con.execute("UPDATE files SET removed_at='x'")
    s = _sync(con)
    assert s._unique("C/W1/spec.pdf", "https://learning.monash.edu/pluginfile.php/9/mod_resource/content/1/spec.pdf") \
        == "C/W1/spec.pdf"
    con.close()


def test_old_db_gets_removed_at_column(tmp_path):
    raw = sqlite3.connect(tmp_path / "t.db")
    raw.execute("CREATE TABLE files (course_id INTEGER NOT NULL, source TEXT NOT NULL, cmid INTEGER,"
                " path TEXT NOT NULL, size INTEGER, section TEXT, title TEXT, synced_at TEXT NOT NULL,"
                " PRIMARY KEY (course_id, source))")
    raw.commit()
    raw.close()
    con = db_connect(tmp_path / "t.db")
    assert "removed_at" in {r["name"] for r in con.execute("PRAGMA table_info(files)")}
    con.close()


def test_removed_files_are_hidden_from_list_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path)
    files = tmp_path / "files"
    (files / "F").mkdir(parents=True)
    (files / "F" / "keep.md").write_text("functor laws", encoding="utf-8")
    (files / "F" / "gone.md").write_text("functor laws, old version", encoding="utf-8")
    con = db_connect()
    _add(con, "a", "F/keep.md")
    _add(con, "b", "F/gone.md")
    con.execute("UPDATE files SET removed_at='x' WHERE source='b'")
    con.commit()
    assert [r["path"] for r in M.t_list_files({})] == ["F/keep.md"]

    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    idx = sqlite3.connect(":memory:")
    CI.index(idx)
    assert [h["path"] for h in CI.search(idx, "functor")] == ["F/keep.md"]
    con.close()
