"""合成的 Moodle 页面和文件，不访问真实接口。"""
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from monash_study_kit import htmlmd, moodlelib, syncer

BASE = "https://learning.monash.edu"


class FakeMoodle:
    def __init__(self, cms, pages=None, data=None):
        self.cms, self.pages, self.data = cms, pages or {}, data or {}
        self.requests = []

    def course_state(self, cid):
        return {"cm": self.cms, "section": [{"number": 1, "title": "Week 1", "cmlist": [c["id"] for c in self.cms],
                                               "sectionurl": BASE + "/course/section.php?id=1"}]}

    def html(self, url):
        self.requests.append(url)
        return self.pages.get(url, "")

    def resolve(self, url):
        self.requests.append(url)
        target = self.pages.get(url, url)
        return target, None, SimpleNamespace(status=200, headers={})

    def download(self, url, dest):
        self.requests.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.data[url])
        return SimpleNamespace(headers={}, url=url)


def cm(kind, n=1, name="Sample"):
    return {"id": n, "name": name, "module": kind, "sectionnumber": 1,
            "url": f"{BASE}/mod/{kind}/view.php?id={n}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(syncer, "FILES_DIR", tmp_path / "files")
    con = moodlelib.db_connect(tmp_path / "m.db")
    yield con, tmp_path / "files"
    con.close()


def run(env, client):
    con, files = env
    s = syncer.CourseSync(client, con, {"id": 1, "fullname": "FIT1000 Sample"}, log=lambda _: None)
    assert not s.run().errors
    return s


def test_page_images_are_local_and_deduplicated(env):
    c = cm("page")
    a, b = [BASE + "/pluginfile.php/1/mod_page/content/1/" + n for n in ("a.png", "b.png")]
    client = FakeMoodle([c], {c["url"]: f'<main id="region-main"><div class="generalbox"><h2>Topic</h2><img src="{a}"><img src="{b}"></div></main>'}, {a: b"image", b: b"image"})
    run(env, client)
    assert len(list(env[1].rglob("assets/*.png"))) == 1
    body = next(env[1].rglob("*.md")).read_text(encoding="utf-8")
    assert "../assets/" in body and a not in body and "## Topic" in body


def test_book_print_only(env):
    c = cm("book")
    url = BASE + "/mod/book/tool/print/index.php?id=1"
    client = FakeMoodle([c], {url: '<div class="book_chapter"><h2>First chapter</h2><p>Made up text.</p></div>'})
    run(env, client)
    assert c["url"] not in client.requests
    assert "## First chapter" in next(env[1].rglob("*.md")).read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["rubric", "guide", "hidden"])
def test_assignment_grading(env, kind):
    c = cm("assign")
    grading = {
        "rubric": '<table class="gradingform_rubric"><tr class="criterion"><td class="description">Clarity</td><td class="level"><span class="definition">Clear</span><span class="score">5 points</span></td></tr></table>',
        "guide": '<table class="gradingform_guide"><tr class="criterion"><td class="criterionshortname">Clarity</td><td class="criteriondescription">Explain steps</td><td class="score">5</td></tr></table>',
        "hidden": ""}[kind]
    run(env, FakeMoodle([c], {c["url"]: '<div id="intro">Invented brief</div>' + grading}))
    body = next(env[1].rglob("*.md")).read_text(encoding="utf-8")
    if kind == "hidden":
        assert env[0].execute("SELECT kind FROM notes").fetchone()[0] == "grading_hidden"
    else:
        assert "| Clarity |" in body
        assert ("| 评分项 | 说明 | 分数 |" if kind == "guide" else "Clear (5)") in body


@pytest.mark.parametrize("wrapper", ["", "Sample/"])
def test_folder_zip_preserves_paths_and_cache(env, wrapper):
    c = cm("folder")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(wrapper + "images/jupytertoolbarsave.PNG", b"image")
        z.writestr(wrapper + "Week1-notebook.ipynb", json.dumps({"cells": [{"source": ["![x](images/jupytertoolbarsave.png)"]}]}))
    url = BASE + "/mod/folder/download_folder.php?id=1"
    client = FakeMoodle([c], data={url: out.getvalue()})
    run(env, client)
    assert next(env[1].rglob("images/jupytertoolbarsave.PNG")).is_file()
    assert next(env[1].rglob("Week1-notebook.ipynb")).is_file()
    assert c["url"] not in client.requests
    run(env, client)
    assert client.requests.count(url) == 1
    assert "只差大小写" in env[0].execute("SELECT reason FROM notes WHERE kind='notebook_image'").fetchone()[0]


@pytest.mark.parametrize("path", ["../escape", "/absolute/file", "a/../../b", "C:\\file"])
def test_zip_paths_stay_relative(path):
    parts = syncer._zip_parts(path)
    assert ".." not in parts and all("/" not in p and "\\" not in p and ":" not in p for p in parts)


def test_folder_html_fallback(env):
    c = cm("folder")
    url = BASE + "/pluginfile.php/1/mod_folder/content/7/images/picture.PNG"
    client = FakeMoodle([c], {c["url"]: f'<a href="{url}">Image</a>'},
                        {BASE + "/mod/folder/download_folder.php?id=1": b"<html>disabled</html>", url: b"image"})
    run(env, client)
    assert next(env[1].rglob("images/picture.PNG")).is_file()


@pytest.mark.parametrize("hidden", [False, True])
def test_internal_pages_from_url_and_label(env, hidden):
    page = cm("page", 2, "Inner")
    outer = cm("url" if not hidden else "label")
    pages = {page["url"]: '<div class="generalbox">Invented inner content</div>'}
    if hidden:
        pages[BASE + "/course/section.php?id=1"] = f'<li class="activity" data-id="1"><a href="{page["url"]}">Inner</a></li>'
    else:
        pages[outer["url"] + "&redirect=1"] = page["url"]
    run(env, FakeMoodle([outer] + ([] if hidden else [page]), pages))
    assert any("Invented inner content" in p.read_text(encoding="utf-8") for p in env[1].rglob("*.md"))
    assert not env[0].execute("SELECT * FROM links WHERE url=?", (page["url"],)).fetchall()


def test_html_markdown_structure():
    md = htmlmd.to_markdown('<h1>Title</h1><ul><li>Outer<ul><li>Inner</li></ul></li></ul><table><tr><th>A</th></tr><tr><td>B</td></tr></table><pre>if ok:\n    run()</pre><a href="https://example.com">Read</a><script>secret()</script>')
    for expected in ("# Title", "- Outer", "  - Inner", "| A |", "| B |", "    run()", "[Read](https://example.com)"):
        assert expected in md
    assert "secret" not in md


def test_decorative_images_are_dropped_but_link_text_stays():
    from monash_study_kit import htmlmd, syncer
    html = ('<p><img src="https://learning.monash.edu/theme/image.php/monash/core/1/f/pdf" alt="spec.pdf">'
            '<a href="https://learning.monash.edu/pluginfile.php/1/mod_assign/intro/spec.pdf">spec.pdf</a></p>'
            '<p><a href="https://monash.au.panopto.com/Panopto/Pages/Viewer.aspx?id=1">'
            '<img src="https://monash.au.panopto.com/Panopto/Services/FrameGrabber.svc/x.jpg" alt="Week 1 lecture"></a></p>')
    on_image = lambda u: "" if syncer.is_decorative(u) else u  # noqa: E731
    md = htmlmd.to_markdown(html, "https://learning.monash.edu/", on_image=on_image)
    assert "![" not in md
    assert "[Week 1 lecture](https://monash.au.panopto.com/Panopto/Pages/Viewer.aspx?id=1)" in md
    assert syncer.is_decorative("https://learning.monash.edu/theme/image.php/x") and \
        not syncer.is_decorative("https://learning.monash.edu/pluginfile.php/1/mod_page/content/2/a.png")


def test_only_content_links_are_listed_and_stale_links_pruned(tmp_path, monkeypatch):
    from monash_study_kit import htmldom, moodlelib, syncer
    con = moodlelib.db_connect(tmp_path / "m.db")
    s = syncer.CourseSync(None, con, {"id": 1, "fullname": "FIT1043 Data - S2 2025"})
    con.execute("INSERT INTO links (course_id, folder, section, title, url, synced_at) VALUES (1,'W1','s','old','https://moodle.com/app','x')")
    body = htmldom.parse('<div><a href="https://monash.au.panopto.com/Panopto/Pages/Viewer.aspx?id=1">Week 1 recording</a>'
                         '<a href="/mod/page/view.php?id=5">internal</a></div>')
    s.content_links(body, "W1", "Week 1")
    s.prune_links()
    rows = [(r["title"], r["url"]) for r in con.execute("SELECT title, url FROM links")]
    assert rows == [("Week 1 recording", "https://monash.au.panopto.com/Panopto/Pages/Viewer.aspx?id=1")]


def test_in_page_anchors_are_plain_text():
    from monash_study_kit import htmlmd
    md = htmlmd.to_markdown('<ol><li><a href="#respondus">Respondus</a></li></ol>', "https://learning.monash.edu/mod/page/view.php?id=1")
    assert md.strip() == "1. Respondus"
