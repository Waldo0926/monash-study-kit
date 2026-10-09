"""课程笔记网页：挑哪些页、HTML 转文字、抓取节奏（多久重抓、失败重试）。Ed 的 document 页正文。"""
from datetime import datetime, timedelta, timezone

from monash_study_kit import lessons, moodlelib, webnotes as W


def test_choose_keeps_notes_sites_and_drops_the_rest():
    links = [("FIT2102", "https://tgdwyer.github.io/haskell3/#functor", "Functor"),
             ("FIT2102", "https://tgdwyer.github.io/haskell3/", "dup without fragment"),
             ("FIT2102", "https://www.monash.edu/students/admin", "Admin"),
             ("FIT2102", "https://youtu.be/abc", "Video"),
             ("FIT2102", "https://learning.monash.edu/mod/resource/view.php?id=1", "Moodle"),
             ("FIT2102", "https://docs.google.com/document/d/x", "Doc"),
             ("FIT2102", "https://tgdwyer.github.io/files/a1.zip", "Zip"),
             ("FIT2102", "https://example.org/one-off", "Random page")]
    links += [("FIT2109", f"https://notes.example.net/w{i}.html", f"W{i}") for i in range(5)]  # 同一站链接 5 次
    got = W.choose(links)
    assert [(c, u) for c, u, _ in got if c == "FIT2102"] == [("FIT2102", "https://tgdwyer.github.io/haskell3/")]
    assert len([1 for c, *_ in got if c == "FIT2109"]) == 5


HTML = """<html><head><title>Site · Page</title></head><body>
<nav>Home | Next</nav>
<main><h1>Functor and Applicative</h1>
<p>Operator <span class="glossary-term">sectioning<span class="glossary-popup">The process of partially
applying</span></span> is handy.</p>
<h2>Example</h2><ul><li>first</li><li>second <code>fmap</code></li></ul>
<pre><code>instance Functor Maybe where
  fmap f (Just x) = Just (f x)</code></pre>
<script>track()</script></main><footer>© 2026</footer></body></html>"""


def test_html_to_text_keeps_structure_and_drops_chrome():
    title, text = W.html_to_text(HTML)
    assert title == "Functor and Applicative"
    assert "Operator sectioning is handy." in text and "partially" not in text     # 术语弹窗去掉
    assert "## Example" in text and "- first" in text and "- second `fmap`" in text
    assert "```\ninstance Functor Maybe where\n  fmap f (Just x) = Just (f x)\n```" in text  # 代码缩进保留
    assert "Home | Next" not in text and "track()" not in text and "2026" not in text


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(W, "ED_DB", tmp_path / "no-ed.db")
    monkeypatch.setattr(W, "GAP", 0)
    con = moodlelib.db_connect(tmp_path / "m.db")
    con.execute("INSERT INTO courses (id, code, folder, fullname, tracked) VALUES (7, 'FIT2102', 'FIT2102 PP (S2 2026)', 'x', 1)")
    con.execute("INSERT INTO links VALUES (7, 'Week 08', 's', 'Functor', 'https://tgdwyer.github.io/haskell3/', 'now')")
    con.commit()
    return con


def test_sync_writes_registers_and_respects_refresh(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    calls = []

    def fake(url, etag=None, last_modified=None):
        calls.append((url, etag))
        return {"status": "ok", "html": HTML.replace("is handy.", "is handy. " + "x " * 200), "etag": '"v1"'}

    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert W.sync(con, log=lambda m: None, now=now, fetcher=fake)["fetched"] == 1
    path = con.execute("SELECT path FROM files WHERE source LIKE 'web:%'").fetchone()["path"]
    assert path == "FIT2102 PP (S2 2026)/Course notes (web)/Functor and Applicative.md"
    body = (tmp_path / "files" / path).read_text(encoding="utf-8")
    assert body.startswith("# Functor and Applicative\n\n来源：https://tgdwyer.github.io/haskell3/")

    W.sync(con, log=lambda m: None, now=now + timedelta(days=3), fetcher=fake)          # 7 天内不重抓
    assert len(calls) == 1
    W.sync(con, log=lambda m: None, now=now + timedelta(days=8),
           fetcher=lambda u, e=None, l=None: calls.append((u, e)) or {"status": "unchanged"})
    assert calls[-1][1] == '"v1"'                                                        # 条件请求带 ETag
    assert con.execute("SELECT path FROM web_pages").fetchone()["path"] == path          # 304 不丢路径


def test_failed_pages_retry_next_day(tmp_path, monkeypatch):
    con = _setup(tmp_path, monkeypatch)
    calls = []
    down = lambda u, e=None, l=None: calls.append(u) or {"status": "error", "reason": "HTTP 503"}  # noqa: E731
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert W.sync(con, log=lambda m: None, now=now, fetcher=down)["errors"] == 1
    W.sync(con, log=lambda m: None, now=now + timedelta(hours=6), fetcher=down)
    W.sync(con, log=lambda m: None, now=now + timedelta(days=1, hours=1), fetcher=down)
    assert len(calls) == 2


def test_ed_document_slides_become_text():
    doc = '<document version="2.0"><paragraph>A <bold>pipe</bold> connects stdout of one command ' \
          'to stdin of the next, e.g. ls | wc -l.</paragraph></document>'
    assert "A **pipe** connects" in lessons.slide_text({"type": "document", "content": doc})
    assert lessons.slide_text({"type": "document", "data": {"passage": doc}}).startswith("A **pipe**")
    link_only = '<document version="2.0"><paragraph>https://x/y</paragraph></document>'
    assert lessons.slide_text({"type": "document", "content": link_only}) == ""       # 只有链接的页不存
    files = lessons.fetch_lesson_files(None, 1, [{"id": 9, "type": "document", "title": "Pipes", "content": doc},
                                                 {"id": 10, "type": "quiz", "title": "Q"}])
    assert [(f["id"], f["type"]) for f in files] == [(9, "document")] and "pipe" in files[0]["text"]


def test_robots_txt_is_respected_and_cached(monkeypatch):
    import urllib.robotparser
    monkeypatch.setattr(W, "_robots", {})
    loads = []

    def loader(site):
        loads.append(site)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(["User-agent: *", "Disallow: /drafts/"])
        return rp

    assert W.robots_allowed("https://tgdwyer.github.io/functionaljavascript/", loader)
    assert not W.robots_allowed("https://tgdwyer.github.io/drafts/a2/", loader)
    assert loads == ["https://tgdwyer.github.io"]                      # 同一个站只取一次
    assert W.robots_allowed("https://x.github.io/a", lambda site: None)  # 取不到 robots.txt 就不拦
