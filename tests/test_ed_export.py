"""Ed Lessons 的课件：按 课号/周/lesson 存、正文里附的文件一起下载、整门课打包成 zip。

假的 Ed 接口和假的下载，数据都是合成的。"""
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from monash_study_kit import cli, content_index as CI, edexport, edlib, library, mcp_server as M
from monash_study_kit import lessons as L

OPEN = urllib.request.urlopen          # 真的 urlopen：fixture 会把模块里的换成假的下载
PDF1 = "https://static.edusercontent.com/files/AAAA"
PDF2 = "https://static.edusercontent.com/files/BBBB"
STARTER = "https://static.edusercontent.com/files/CCCC"
DOC = (f'<document version="2.0"><paragraph>Download the starter code before the workshop and read the brief.</paragraph>'
       f'<file url="{STARTER}" filename="starter.zip"/>'
       '<file url="https://evil.example/files/x" filename="not-ed.zip"/></document>')


class FakeEd:
    def __init__(self):
        self.slides = {
            11: [{"id": 101, "index": 0, "type": "webpage", "title": "Reading", "url": "https://notes.example/w1"}],
            12: [{"id": 201, "index": 0, "type": "pdf", "title": "Workshop 3 Slides.pdf", "file_url": PDF1},
                 {"id": 202, "index": 1, "type": "document", "title": "Instructions", "content": DOC},
                 {"id": 203, "index": 2, "type": "quiz", "title": "Check"}],
        }

    def get(self, path, params=None):
        if path == "/courses/4242/lessons":
            return {"modules": [{"id": 2, "name": "Week 3: Editor/Modern IDE"}, {"id": 1, "name": "Week 1: Shell"}],
                    "lessons": [{"id": 12, "module_id": 2, "index": 1, "title": "W3 Workshop", "status": "completed"},
                                {"id": 11, "module_id": 1, "index": 0, "title": "W1 Pre-Class", "status": "attempted"}]}
        if path.startswith("/lessons/slides/"):
            return {"questions": []}
        lid = int(path.rsplit("/", 1)[1])
        return {"lesson": {"slides": self.slides[lid]}}


class Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def ed(tmp_path, monkeypatch):
    files = tmp_path / "ed-files"
    for mod in (L, CI, edexport):
        monkeypatch.setattr(mod, "ED_FILES_DIR" if mod is not L else "FILES_DIR", files)
    fetched = []

    def urlopen(req, timeout=None):
        fetched.append(req.full_url)
        return Body(f"content of {req.full_url}".encode())

    monkeypatch.setattr(L.urllib.request, "urlopen", urlopen)
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses (id, code, name, tracked) VALUES (4242, 'FIT2109 S2 2026 Malaysia', 'Workshop', 1)")
    conn.commit()
    yield conn, files, fetched, FakeEd()
    conn.close()


def _paths(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT title, local_path FROM lesson_files")}


def test_files_use_course_code_week_and_lesson_names(ed):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    paths = _paths(conn)
    week3 = "FIT2109/02 Week 3 - Editor-Modern IDE/02 W3 Workshop"
    assert paths["Workshop 3 Slides.pdf"] == f"{week3}/01 Workshop 3 Slides.pdf"
    assert paths["Instructions"] == f"{week3}/02 Instructions.md"
    # 正文里附的 Ed 文件也下载了；别的网站的链接不碰
    assert paths["starter.zip"] == f"{week3}/02 starter.zip"
    assert (files / paths["starter.zip"]).read_bytes() == f"content of {STARTER}".encode()
    assert not any("evil.example" in u for u in fetched)
    assert paths["Reading"] is None                      # 网页只记链接
    assert sorted(fetched) == [PDF1, STARTER]


def test_old_numeric_layout_is_moved_not_downloaded_again(ed):
    conn, files, fetched, client = ed
    L.ensure_lessons_schema(conn)
    old = files / "4242" / "12" / "201_Workshop 3 Slides.pdf.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"downloaded by 1.5")
    conn.execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title, file_url, local_path, idx) "
                 "VALUES (201, 12, 4242, 'pdf', 'Workshop 3 Slides.pdf', ?, '4242/12/201_Workshop 3 Slides.pdf.pdf', 0)",
                 (PDF1,))
    conn.commit()
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    new = files / _paths(conn)["Workshop 3 Slides.pdf"]
    assert new.read_bytes() == b"downloaded by 1.5" and PDF1 not in fetched
    assert not old.exists() and not (files / "4242").exists()      # 空了的旧文件夹也删掉


def test_replaced_file_is_downloaded_again_and_removed_slide_is_deleted(ed):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    starter = files / _paths(conn)["starter.zip"]
    client.slides[12][0]["file_url"] = PDF2                        # 老师换了 PDF
    client.slides[12] = [s for s in client.slides[12] if s["type"] != "document"]   # 删了说明页
    fetched.clear()
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    assert fetched == [PDF2]
    assert (files / _paths(conn)["Workshop 3 Slides.pdf"]).read_bytes() == f"content of {PDF2}".encode()
    assert not starter.exists() and "starter.zip" not in _paths(conn)


def test_oversized_attachment_is_skipped(ed, monkeypatch):
    conn, files, fetched, client = ed
    monkeypatch.setattr(L, "ATTACHMENT_MAX", 5)
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    assert _paths(conn)["starter.zip"] is None and _paths(conn)["Workshop 3 Slides.pdf"]
    assert not list(files.rglob("*.part"))


def test_export_zip_is_grouped_by_week_with_links_and_readme(ed, tmp_path):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    d = edexport.export(conn, "FIT2109", tmp_path / "out")
    assert d["files"] == 3 and d["lessons"] == 2 and d["missing"] == 0
    assert d["path"].endswith(".zip") and "FIT2109 Ed Lessons" in d["path"]
    with zipfile.ZipFile(d["path"]) as z:
        names = set(z.namelist())
        root = "FIT2109 Ed Lessons"
        assert f"{root}/02 Week 3 - Editor-Modern IDE/02 W3 Workshop/01 Workshop 3 Slides.pdf" in names
        assert f"{root}/02 Week 3 - Editor-Modern IDE/02 W3 Workshop/02 starter.zip" in names
        links = z.read(f"{root}/01 Week 1 - Shell/01 W1 Pre-Class/Links.md").decode()
        assert "https://notes.example/w1" in links and "edstem.org/au/courses/4242/lessons/11" in links
        readme = z.read(f"{root}/README.md").decode()
        assert "## Week 1: Shell" in readme and "W3 Workshop" in readme and "不要再公开分发" in readme
    only = edexport.export(conn, "FIT2109", tmp_path / "out", module="Week 3")
    with zipfile.ZipFile(only["path"]) as z:
        assert not any("Week 1" in n for n in z.namelist())


def test_export_lists_missing_files_and_needs_a_sync_first(ed, tmp_path):
    conn, files, fetched, client = ed
    with pytest.raises(LookupError, match="monash sync"):
        edexport.export(conn, "FIT2109", tmp_path)
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    (files / _paths(conn)["Workshop 3 Slides.pdf"]).unlink()
    d = edexport.export(conn, "FIT2109", tmp_path)
    with zipfile.ZipFile(d["path"]) as z:
        readme = z.read("FIT2109 Ed Lessons/README.md").decode()
    assert d["missing"] == 1 and "没下载到" in readme and "Workshop 3 Slides.pdf" in readme
    with pytest.raises(LookupError):
        edexport.export(conn, "FIT2109", tmp_path, module="Week 9")


def test_course_folder_names():
    assert L.course_folder("FIT3162/FIT3164 MUM S2 2026", 1) == "FIT3162+FIT3164"
    assert L.course_folder(None, 38469) == "38469"
    assert L._label('Lab: "a/b"?.', "x") == "Lab - -a-b-"
    assert L.is_ed_file_url(STARTER) and not L.is_ed_file_url("https://edusercontent.com.evil.example/x")


def test_library_lists_synced_ed_files_and_serves_the_zip(ed, tmp_path, monkeypatch):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    monkeypatch.setattr(library, "DATA_DIR", tmp_path)
    monkeypatch.setattr(library, "ED_DB", tmp_path / "ed.db")
    monkeypatch.setattr(library, "ED_DIR", files)
    srv = library.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        page = OPEN(base + "/", timeout=5).read().decode()
        assert "Workshop 3 Slides.pdf" in page and "Ed · Week 3: Editor/Modern IDE · W3 Workshop" in page
        assert "/export?course=4242" in page
        with OPEN(base + "/export?course=4242", timeout=5) as r:
            assert r.headers["Content-Type"] == "application/zip"
            body = r.read()
        assert "FIT2109 Ed Lessons/README.md" in zipfile.ZipFile(io.BytesIO(body)).namelist()
        with pytest.raises(urllib.error.HTTPError) as e:
            OPEN(base + "/export?course=1", timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_mcp_lists_ed_files_and_exports(ed, tmp_path, monkeypatch):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    monkeypatch.setattr(M, "_ed", lambda: edlib.db_connect(tmp_path / "ed.db"))
    monkeypatch.setattr(edexport, "default_dir", lambda: tmp_path / "Downloads")
    rows = M._ed_files({"course": "FIT2109", "week": 3})
    assert {r["path"] for r in rows} == {f"ed:{p}" for t, p in _paths(conn).items() if p}
    assert M._ed_files({"course": "FIT2109", "week": 1}) == []
    assert [r["activity"] for r in M._ed_files({"query": "starter"})] == ["Ed · Week 3: Editor/Modern IDE · W3 Workshop"]
    result = M.call_tool("export_ed_lessons", {"course": "FIT2109"})
    d = json.loads(result["content"][0]["text"])
    assert d["files"] == 3 and (tmp_path / "Downloads").joinpath(d["path"].rsplit("/", 1)[-1]).exists()


def test_cli_export(ed, tmp_path, monkeypatch, capsys):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda m: None)
    monkeypatch.setattr(edlib, "db_connect", lambda *a, **k: conn)
    assert cli.main(["ed", "export", "FIT2109", "--to", str(tmp_path / "out"), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["files"] == 3 and d["path"].startswith(str(tmp_path / "out"))


def test_document_images_cached_and_export_paths_resolve(ed, tmp_path):
    import posixpath
    from monash_study_kit import export as X
    conn, files, fetched, client = ed
    image = "https://static.edusercontent.com/images/sample.png"
    client.slides[12][1]["content"] = '<document><paragraph>Invented instructions for the sample image in this lesson.</paragraph><image src="' + image + '"/></document>'
    L.sync_lessons(conn, client, 4242, log=lambda _: None)
    L.sync_lessons(conn, client, 4242, log=lambda _: None)
    md = files / _paths(conn)["Instructions"]
    body = md.read_text(encoding="utf-8")
    assert image not in body and "../../assets/" in body
    assert fetched.count(image) == 1
    assert conn.execute("SELECT sha FROM ed_assets WHERE url=?", (image,)).fetchone()[0]
    plans = [edexport.plan(conn, "FIT2109")]
    for combined in (False, True):
        if combined:
            p = X.plan("FIT2109", moodle_con=None, ed_conn=conn)
            out = io.BytesIO()
            X.write_zip(p, out)
        else:
            out = io.BytesIO()
            edexport.write_zip(plans[0], out)
        with zipfile.ZipFile(out) as z:
            name = next(n for n in z.namelist() if n.endswith("Instructions.md"))
            import re
            ref = re.search(r"!\[[^\]]*\]\(([^)]+)\)", z.read(name).decode())[1]
            assert posixpath.normpath(posixpath.join(posixpath.dirname(name), ref)) in z.namelist()


def test_document_image_failure_is_kept_and_indexed(ed, monkeypatch):
    from monash_study_kit import export as X
    conn, files, fetched, client = ed
    image = "https://static.edusercontent.com/images/missing.png"
    client.slides[12][1]["content"] = '<document><paragraph>Invented instructions for a missing image in this lesson.</paragraph><image src="' + image + '"/></document>'
    monkeypatch.setattr(L, "download_pdf", lambda *a, **kw: False)
    L.sync_lessons(conn, client, 4242, log=lambda _: None)
    assert image in (files / _paths(conn)["Instructions"]).read_text(encoding="utf-8")
    assert conn.execute("SELECT error FROM ed_assets WHERE url=?", (image,)).fetchone()[0]
    p = X.plan("FIT2109", moodle_con=None, ed_conn=conn)
    assert any(m["url"] == image and "失败" in m["reason"] for m in p.missing)
    assert p.statistics["remote_images"] == 1


def test_webpage_google_file_survives_lessons_resync(ed):
    conn, files, fetched, client = ed
    L.sync_lessons(conn, client, 4242, log=lambda _:None)
    path=files/"FIT2109/01 Week 1 - Shell/01 W1 Pre-Class/Reading.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"google export")
    rel=path.relative_to(files).as_posix()
    conn.execute("UPDATE lesson_files SET local_path=? WHERE id=101",(rel,))
    L.sync_lessons(conn,client,4242,log=lambda _:None)
    assert path.is_file() and _paths(conn)["Reading"] == rel


def test_same_image_content_deduplicates_and_failed_image_retries(ed, monkeypatch):
    conn, files, fetched, client = ed
    L.ensure_lessons_schema(conn)
    image1="https://static.edusercontent.com/a.png"
    image2="https://static.edusercontent.com/b.jpg"
    calls=[]
    def download(url,dest,*a,**kw):
        calls.append(url)
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(b"same image")
        return True
    monkeypatch.setattr(L,"download_pdf",download)
    text=f"![A]({image1}) ![B]({image2})"
    body=L.localize_images(conn,text,"FIT2109",files/"FIT2109/module/lesson")
    assert len(list((files/"FIT2109/assets").iterdir())) == 1
    assert body.count("../../assets/") == 2
    assert len(calls) == 2
    conn.execute("INSERT INTO ed_assets VALUES ('https://static.edusercontent.com/f.png',NULL,NULL,'上次失败','x')")
    assert "https://" not in L.localize_images(conn,"![C](https://static.edusercontent.com/f.png)","FIT2109",files/"FIT2109/module/lesson")


def test_code_challenge_workspace_is_listed_not_fetched(tmp_path, monkeypatch):
    """code 页的工作区要“连接”才读得到：不请求，只在索引里说明。"""
    slides = [{"id": 301, "index": 0, "type": "code", "title": "Exercise 1", "challenge_id": 77,
               "content": "<document><paragraph>Write a function that returns the sum of a list of numbers.</paragraph></document>"}]
    files = L.fetch_lesson_files(None, 9, slides)
    ch = [f for f in files if f["type"] == "challenge"]
    assert len(ch) == 1 and ch[0]["title"] == "Exercise 1" and ch[0]["file_url"] is None
    assert edexport.CHALLENGE_REASON.startswith("Code challenge")


def test_images_without_an_extension_get_one_from_their_content(tmp_path, monkeypatch):
    """Ed 的图片地址没有扩展名：按文件头存成 .png；旧版存成 .img 的，下次同步改名并更新 .md 引用。"""
    assert L.image_ext(b"\x89PNG\r\n\x1a\n...") == ".png" and L.image_ext(b"\xff\xd8\xff\xe0") == ".jpg"
    assert L.image_ext(b"RIFF\x00\x00\x00\x00WEBPVP8") == ".webp" and L.image_ext(b"<svg xmlns='x'>") == ".svg"
    assert L.image_ext(b"hello") is None
    files = tmp_path / "ed-files"
    monkeypatch.setattr(L, "FILES_DIR", files)
    conn = edlib.db_connect(tmp_path / "ed.db")
    L.ensure_lessons_schema(conn)
    (files / "FIT1043" / "assets").mkdir(parents=True)
    (files / "FIT1043" / "assets" / "abc.img").write_bytes(b"\x89PNG\r\n\x1a\nrest")
    md = files / "FIT1043" / "01 Week 1" / "01 Intro" / "02 Definition.md"
    md.parent.mkdir(parents=True)
    md.write_text("See ![图片](../../assets/abc.img)\n", encoding="utf-8")
    conn.execute("INSERT INTO ed_assets VALUES ('https://static.au.edusercontent.com/files/X','FIT1043/assets/abc.img','s',NULL,'x')")
    assert L.repair_image_names(conn) == 1
    assert (files / "FIT1043" / "assets" / "abc.png").is_file() and not (files / "FIT1043" / "assets" / "abc.img").exists()
    assert "../../assets/abc.png)" in md.read_text(encoding="utf-8")
    assert conn.execute("SELECT local_path FROM ed_assets").fetchone()[0] == "FIT1043/assets/abc.png"
    assert L.repair_image_names(conn) == 0
