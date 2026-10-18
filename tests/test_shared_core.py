"""先在服务器部署里做出来、合进核心包的功能：zip 内容搜索、新课件、下架文件、中断的下载、环境变量配置。"""
import io
import sqlite3
import zipfile

import pytest

from monash_study_kit import content_index as CI
from monash_study_kit import features as F
from monash_study_kit import mcp_server as M
from monash_study_kit import moodlelib, paths, syncer, todo
from monash_study_kit import transcribe as T
from monash_study_kit.moodlelib import db_connect


def _add(con, source, path, cmid=1, synced="2026-10-01T00:00:00+00:00"):
    con.execute("INSERT INTO files (course_id, source, cmid, path, size, synced_at) VALUES (1, ?, ?, ?, 10, ?)",
                (source, cmid, path, synced))


def _sync(con):
    return syncer.CourseSync(None, con, {"id": 1, "fullname": "FIT2102 Paradigms - S2 2026"})


def test_zip_contents_are_searchable(tmp_path, monkeypatch):
    files = tmp_path / "files"
    week = files / "FIT2102 Paradigms (S2 2026)" / "Week 10"
    week.mkdir(parents=True)
    with zipfile.ZipFile(week / "applied10.zip", "w") as z:
        z.writestr("applied10/src/MaybeMonad.hs", "-- | Exercise: implement bind for the Maybe monad\nbind = undefined")
        z.writestr("applied10/data/big.csv", "x" * (CI.ZIP_MEMBER_MAX + 1))
        z.writestr("__MACOSX/applied10/._MaybeMonad.hs", "junk monad")
    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    con = sqlite3.connect(":memory:")
    CI.index(con)
    hits = CI.search(con, "bind maybe")
    assert len(hits) == 1 and hits[0]["inner"] == "applied10/src/MaybeMonad.hs"
    assert hits[0]["path"] == "FIT2102 Paradigms (S2 2026)/Week 10/applied10.zip"


def _ed_fixture(tmp_path):
    """ed-digest 那种库：除了 lesson_files 还有 lesson_texts（文字页、抓下来的阅读网页）。"""
    ed = tmp_path / "ed"
    (ed / "files" / "39026" / "115720").mkdir(parents=True)
    (ed / "files" / "39026" / "115720" / "797215_Vim Basics.md").write_text(
        "# 3.6 Vim Basics\n\nNormal mode and insert mode.", encoding="utf-8")
    con = sqlite3.connect(ed / "ed.db")
    con.executescript("""
        CREATE TABLE courses (id INTEGER PRIMARY KEY, code TEXT);
        CREATE TABLE lessons (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE lesson_files (lesson_id INTEGER, course_id INTEGER, title TEXT, file_url TEXT, local_path TEXT);
        CREATE TABLE lesson_texts (slide_id INTEGER, lesson_id INTEGER, course_id INTEGER, title TEXT, url TEXT,
                                   local_path TEXT);
        INSERT INTO courses VALUES (39026, 'FIT2109 S2 2026 Malaysia ');
        INSERT INTO lessons VALUES (115720, 'W3 Pre-Class');
        INSERT INTO lesson_texts VALUES (797215, 115720, 39026, '3.6 Vim Basics', 'https://example.github.io/w3/3.6.html',
                                         '39026/115720/797215_Vim Basics.md');
    """)
    con.commit()
    con.close()
    return ed


def test_ed_readings_are_labelled_and_readable(tmp_path, monkeypatch):
    ed = _ed_fixture(tmp_path)
    monkeypatch.setattr(CI, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(CI, "ED_FILES_DIR", ed / "files")
    monkeypatch.setattr(CI, "ED_DB", ed / "ed.db")
    con = sqlite3.connect(":memory:")
    CI.index(con)
    hit = CI.search(con, "insert mode")[0]
    assert (hit["course"], hit["source"], hit["title"]) == ("FIT2109", "ed", "W3 Pre-Class · 3.6 Vim Basics")
    assert hit["url"] == "https://example.github.io/w3/3.6.html"
    assert hit["path"] == "ed:39026/115720/797215_Vim Basics.md"
    assert CI.resolve_readable(hit["path"]).read_text(encoding="utf-8").startswith("# 3.6 Vim Basics")
    for bad in ("ed:../ed.db", "../ed/ed.db", "ed:39026/115720/missing.md"):
        with pytest.raises(ValueError):
            CI.resolve_readable(bad)


def test_old_index_without_url_column_is_migrated():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE content_files (path TEXT PRIMARY KEY, source TEXT, course TEXT, title TEXT,"
                " mtime REAL, size INTEGER, chunks INTEGER, indexed_at TEXT)")
    CI.ensure_schema(con)
    assert "url" in {r[1] for r in con.execute("PRAGMA table_info(content_files)")}


def test_list_files_since_days_and_removed_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path)
    con = db_connect()
    con.execute("INSERT INTO courses (id, code, folder, fullname) VALUES (1, 'FIT2102', 'F', 'x')")
    _add(con, "a", "F/old.pdf", synced="2026-01-01T00:00:00+00:00")
    _add(con, "b", "F/new.pdf", synced=moodlelib.now_iso())
    _add(con, "c", "F/gone.pdf", synced=moodlelib.now_iso())
    con.execute("UPDATE files SET removed_at='x' WHERE source='c'")
    con.commit()
    assert {r["path"] for r in M.t_list_files({})} == {"F/old.pdf", "F/new.pdf"}
    assert [r["path"] for r in M.t_list_files({"since_days": 7})] == ["F/new.pdf"]
    assert [f["path"] for f in todo.new_files([{"id": 1}], 7)] == ["F/new.pdf"]


def test_transcript_follows_its_video_when_removed(tmp_path):
    con = db_connect(tmp_path / "t.db")
    _add(con, "https://x/lec.mp4", "C/lec.mp4")
    _add(con, "transcript:C/lec.mp4", "C/lec.transcript.md", cmid=None)
    s = _sync(con)
    s.mark_removed()
    assert moodlelib.removed_paths(con) == {"C/lec.mp4", "C/lec.transcript.md"}
    s = _sync(con)
    s.seen_sources = {"https://x/lec.mp4"}
    s.mark_removed()
    assert moodlelib.removed_paths(con) == set()


def test_clean_partials_removes_leftovers(tmp_path, monkeypatch):
    monkeypatch.setattr(syncer, "FILES_DIR", tmp_path)
    s = _sync(None)
    week = tmp_path / s.root / "Week 03"
    week.mkdir(parents=True)
    (week / ".dl-12.part.part").write_bytes(b"half")
    (week / "applied3.zip").write_bytes(b"ok")
    s.clean_partials()
    assert [p.name for p in week.iterdir()] == ["applied3.zip"]


def test_interrupted_download_leaves_no_partial_file(tmp_path, monkeypatch):
    class Broken(io.BytesIO):
        status = 200
        headers = {}

        def read(self, n=-1):
            if self.tell():
                raise ConnectionResetError("断了")
            return super().read(5)

    resp = Broken(b"x" * 100)
    resp.headers = type("H", (), {"get": lambda self, k, d=None: None, "get_all": lambda self, k: []})()
    monkeypatch.setattr(moodlelib, "REQUEST_GAP", 0)
    monkeypatch.setattr(moodlelib._opener, "open", lambda req, timeout: resp)
    client = moodlelib.MoodleClient(jar={"MoodleSession": "x"}, persist=False)
    with pytest.raises(ConnectionResetError):
        client._request("https://example.test/f.zip", stream_to=tmp_path / ".dl-7")
    assert list(tmp_path.iterdir()) == []


def test_cookie_save_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(moodlelib, "COOKIE_FILE", tmp_path / "moodle-cookie")
    moodlelib.save_cookies({"MoodleSession": "abc"})
    assert [p.name for p in tmp_path.iterdir()] == ["moodle-cookie"]
    assert moodlelib.load_cookies() == {"MoodleSession": "abc"}


def test_transcribe_caches_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "FILES_DIR", tmp_path)
    con = db_connect(tmp_path / "t.db")
    for name in ("keep.mp4", "gone.mp4"):
        (tmp_path / name).write_bytes(b"v" * 100)
        _add(con, name, name)
    con.execute("UPDATE files SET removed_at='x' WHERE path='gone.mp4'")
    assert [p["path"] for p in T.pending(con)] == ["keep.mp4"]
    calls = []
    real = T.file_hash
    monkeypatch.setattr(T, "file_hash", lambda p: calls.append(p) or real(p))
    h = T.cached_hash(con, "keep.mp4")
    assert T.cached_hash(con, "keep.mp4") == h and len(calls) == 1   # 第二次走缓存
    (tmp_path / "keep.mp4").write_bytes(b"w" * 200)
    assert T.cached_hash(con, "keep.mp4") != h and len(calls) == 2   # 文件变了就重算


def test_discussion_rows_without_id_are_skipped():
    class C:
        def html(self, url):
            return ('<table><tr data-region="discussion-list-item"><td><a class="w-100" href="discuss.php?d=1">'
                    'pinned divider</a></td></tr><tr data-region="discussion-list-item" data-discussionid="42">'
                    '<td><a class="w-100" href="discuss.php?d=42">Week 9 notes</a></td></tr></table>')
    assert [d["id"] for d in F.discussions(C(), 1)] == [42]


def test_ical_activity_strips_monash_suffixes():
    assert F.ical_activity("Week 8 Applied Session Submission - Allocate+ FIT2102 Tutorial 09 MA_Active "
                           "Learning Classroom 6315 Wed 14:00 (Due date)") == "Week 8 Applied Session Submission"
    assert F.ical_activity("Assignment 2 - CALLISTA: FIT2109 MALAYSIA ON-CAMPUS S2 2026 (Due date)") == "Assignment 2"
    assert F.ical_activity("Group Project Meeting Minutes & Video 4 (week 10) is due") == \
        "Group Project Meeting Minutes & Video 4 (week 10)"
    assert F.ical_activity("Pre-reading - chapter 3") == "Pre-reading - chapter 3"      # 普通的“ - ”不动


def test_settings_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("MONASH_KIT_DOWNLOAD_VIDEOS", "true")
    monkeypatch.setenv("MONASH_KIT_NAME_LIMIT", "120")
    monkeypatch.setenv("MONASH_KIT_PDF_EXTRACTOR", "pdftotext")
    s = paths.load_settings()
    assert s["download_videos"] is True and s["name_limit"] == 120 and s["pdf_extractor"] == "pdftotext"
    monkeypatch.setenv("MONASH_KIT_NAME_LIMIT", "abc")          # 写错了就用默认值
    assert paths.load_settings()["name_limit"] == 80


def test_safe_name_limit_is_configurable():
    long = "Week 07 " + "very long lecture title " * 8 + ".pdf"
    assert len(syncer.safe_name(long)) <= 80
    assert len(syncer.safe_name(long, 120)) > 80 and syncer.safe_name(long, 120).endswith(".pdf")


def test_server_layout_env_vars(tmp_path):
    """服务器部署把数据、凭据、Ed 库放在各自的目录：paths 在导入时读环境变量，所以起个新进程看。"""
    import json
    import os
    import subprocess
    import sys
    env = {**os.environ, "MONASH_KIT_DATA": str(tmp_path / "data"), "MONASH_KIT_SECRETS": str(tmp_path / "cfg"),
           "MONASH_KIT_ED_DATA": str(tmp_path / "ed"), "MONASH_KIT_ED_TOKEN": str(tmp_path / "ed-token")}
    code = ("import json; from monash_study_kit import edlib, moodlelib, paths; "
            "print(json.dumps([str(moodlelib.COOKIE_FILE), str(paths.DATA_DIR), str(edlib.DB_PATH), "
            "str(paths.ED_FILES_DIR), str(edlib.TOKEN_FILE)]))")
    out = json.loads(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                    check=True).stdout)
    assert out == [str(tmp_path / "cfg" / "moodle-cookie"), str(tmp_path / "data"), str(tmp_path / "ed" / "ed.db"),
                   str(tmp_path / "ed" / "files"), str(tmp_path / "ed-token")]


def test_token_expiry_and_api_tokens_are_not_renewed():
    import base64
    import json

    from monash_study_kit import edlib
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1790000000}).encode()).decode().rstrip("=")
    assert edlib.token_expiry(f"h.{payload}.s").timestamp() == 1790000000
    assert edlib.token_expiry("not-a-jwt") is None
    assert edlib.EdClient(token="abc.def").renew() is None        # API 令牌不过期，不发请求
