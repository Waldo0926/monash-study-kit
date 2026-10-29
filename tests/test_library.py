import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from monash_study_kit import content_index as CI, edlib, library, moodlelib
from monash_study_kit.lessons import ensure_lessons_schema


def request(url, headers=None, data=None):
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


@pytest.fixture
def server(tmp_path, monkeypatch):
    files = tmp_path / "files"
    (files / "FIT2102" / "Week 07").mkdir(parents=True)
    (files / "FIT2102" / "Week 07" / "slides.txt").write_text("functional programming", encoding="utf-8")
    (files / "FIT2102" / "removed.txt").write_text("old", encoding="utf-8")
    (files / "unregistered-secret.txt").write_text("not for download", encoding="utf-8")
    monkeypatch.setattr(library, "DATA_DIR", tmp_path)
    monkeypatch.setattr(library, "ED_DB", tmp_path / "ed.db")
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path)
    monkeypatch.setattr(CI, "FILES_DIR", files)
    conn = moodlelib.db_connect(tmp_path / "moodle.db")
    conn.execute("INSERT INTO courses (id,code,folder) VALUES (1,'FIT2102','Programming paradigms')")
    for path, gone in (("FIT2102/Week 07/slides.txt", None), ("FIT2102/removed.txt", "gone")):
        conn.execute("INSERT INTO files (course_id,source,path,size,title,section,synced_at,removed_at) VALUES (1,?,?,22,?,'Week 07','now',?)",
                     (path, path, '<script>alert(1)</script>', gone))
    conn.commit()
    conn.close()
    srv = library.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", files, tmp_path
    finally:
        srv.shutdown()
        srv.server_close()


def test_library_groups_filters_and_escapes_titles(server):
    base, _, _ = server
    status, headers, body = request(base + "/?course=FIT2102&q=Week+07")
    assert status == 200 and b"FIT2102" in body and b"Week 07" in body
    assert b"&lt;script&gt;" in body and b"<script>" not in body
    assert b"removed.txt" not in body and b"unregistered-secret" not in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert b"No matching files" in request(base + "/?q=missing")[2]


def test_library_download_only_registered_active_files(server):
    base, _, _ = server
    path = urllib.parse.quote("FIT2102/Week 07/slides.txt")
    status, headers, body = request(base + "/download?path=" + path)
    assert status == 200 and body == b"functional programming"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["X-Content-Type-Options"] == "nosniff"
    for rel in ("../secrets/moodle-cookie", "FIT2102/removed.txt", "unregistered-secret.txt", "/etc/passwd"):
        assert request(base + "/download?" + urllib.parse.urlencode({"path": rel}))[0] == 404


def test_library_refuses_rebinding_cross_origin_and_writes(server):
    base, _, _ = server
    for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert request(base + "/", headers)[0] == 403
    assert request(base + "/", data=b"submit=1")[0] == 405
    assert request(base + "/?offset=not-a-number")[0] == 400


def test_ed_lesson_files_are_in_the_same_library(server, monkeypatch):
    base, _, temp = server
    ed_files = temp / "ed-files"
    ed_files.mkdir()
    pdf = ed_files / "reading.pdf"
    pdf.write_bytes(b"fake PDF fixture")
    monkeypatch.setattr(CI, "ED_FILES_DIR", ed_files)
    conn = edlib.db_connect(temp / "ed.db")
    ensure_lessons_schema(conn)
    conn.execute("INSERT INTO courses(id,code,name,tracked) VALUES (2,'FIT2109','Workshop',1)")
    conn.execute("INSERT INTO lessons(id,course_id,title,status) VALUES (7,2,'W3 Pre-Class','unattempted')")
    conn.execute("INSERT INTO lesson_files (lesson_id,type,title,local_path) VALUES (7,'pdf','Reading',?)", (str(pdf),))
    conn.commit()
    conn.close()
    assert b"FIT2109" in request(base + "/")[2]
    assert request(base + "/download?path=ed%3Areading.pdf")[2] == b"fake PDF fixture"


def test_library_refuses_symlinks_outside_files(server):
    base, files, temp = server
    outside = temp / "secret.txt"
    outside.write_text("private", encoding="utf-8")
    path = files / "FIT2102/Week 07/slides.txt"
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert request(base + "/download?path=FIT2102/Week%2007/slides.txt")[0] == 404
