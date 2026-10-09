"""把一门课的 Moodle 内容同步成 课程/周次 的目录。

    FIT2102 Programming paradigms (S2 2026)/
      Week 01 - Introduction to Functional Programming in JavaScript/
        applied1.zip
        links.md          ← 本周的外部链接（Slides、YouTube、课程笔记…）
      Assessments/2. Written/
        A1 spec.pdf       ← 作业说明里的附件

目录结构来自 core_courseformat_get_state：Monash 的每周是一个 "Week N - 标题"
章节，下面挂 Own-time / Real-time / Wrap-up 子章节（`parent` 指向周章节）。
子章节不单独建目录，文件直接放进周目录里，按周浏览更顺手。
"""
from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass, field

from .moodlelib import (FILES_DIR, HOST, MoodleClient, collect_links, filename_from,
                        is_attachment, now_iso)
from .paths import load_settings

WEEK_RE = re.compile(r"^\s*week\s*0*(\d+)\b\s*[-:–—]?\s*(.*)$", re.I)
CODE_RE = re.compile(r"\b([A-Z]{3}\d{4})\b")
SUFFIX_RE = re.compile(r"\s*-\s*((?:MUM\s+)?(?:S[12]|Summer|Winter)\b.*?\d{4}.*|(?:Summer|Winter)\s+MUM\s+\d{4})\s*$",
                       re.I)
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
# 这些活动里没有能下载的东西（论坛、测验、外部工具…），也不去点开它们
SKIP_MODULES = {"forum", "quiz", "lti", "shadow", "feedback", "choice", "attendance",
                "turnitintooltwo", "zoom", "h5pactivity", "scorm", "questionnaire", "workshop"}


def clean(text: str) -> str:
    return " ".join(html.unescape(text or "").split())


def safe_name(name: str, limit: int = 80) -> str:
    """能当文件/目录名用。去掉路径分隔符和 Windows 不认的字符，截短但保留扩展名。

    上限 80：Windows 默认整条路径不能超过 260 个字符，课程/周次/文件夹/文件四层叠起来很容易超。
    """
    name = clean(name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            name = stem[: limit - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:limit].rstrip()
    return name or "untitled"


def course_code(course: dict) -> str | None:
    m = CODE_RE.search(clean(course.get("fullname", ""))) or CODE_RE.search(course.get("shortname", ""))
    return m.group(1) if m else None


def course_folder(course: dict) -> str:
    """"FIT2102 Programming paradigms - S2 2026" → "FIT2102 Programming paradigms (S2 2026)"."""
    full = clean(course.get("fullname", "")) or course.get("shortname", "") or str(course["id"])
    m = SUFFIX_RE.search(full)
    if m:
        full = f"{full[:m.start()].strip()} ({m.group(1).strip()})"
    return safe_name(full)


def week_folder(title: str) -> str | None:
    m = WEEK_RE.match(clean(title))
    if not m:
        return None
    rest = m.group(2).strip()
    return safe_name(f"Week {int(m.group(1)):02d}" + (f" - {rest}" if rest else ""))


@dataclass
class Layout:
    """章节号 → 相对课程目录的文件夹。"""
    folders: dict[int, str] = field(default_factory=dict)
    titles: dict[int, str] = field(default_factory=dict)


def plan_layout(state: dict) -> Layout:
    sections = {s["number"]: s for s in state["section"]}

    def chain(num: int) -> list[dict]:
        out, seen = [], set()
        while num is not None and num in sections and num not in seen:
            seen.add(num)
            out.append(sections[num])
            num = sections[num].get("parent")
        return list(reversed(out))

    layout = Layout()
    for num, sec in sections.items():
        layout.titles[num] = clean(sec.get("title"))
        path = chain(num)
        week = next((week_folder(s["title"]) for s in reversed(path) if week_folder(s["title"])), None)
        if week:
            layout.folders[num] = week
            continue
        # 非周次的章节：保留层级，但跳过只当容器的上级：自己没有内容的（比如 "Learning"），
        # 以及第 0 节（Monash 叫 "Unit dashboard"，Assessments 之类都挂在它下面）
        parts = [safe_name(s["title"]) for i, s in enumerate(path)
                 if i == len(path) - 1 or (s.get("cmlist") and s["number"] != 0)]
        layout.folders[num] = "/".join(parts) or "General"
    return layout


@dataclass
class SyncStats:
    downloaded: int = 0
    skipped: int = 0
    links: int = 0
    errors: list[str] = field(default_factory=list)
    bytes: int = 0


class CourseSync:
    def __init__(self, client: MoodleClient, con, course: dict, *, dry_run: bool = False, log=print):
        self.c = client
        self.con = con
        self.course = course
        self.cid = int(course["id"])
        self.root = course_folder(course)
        self.dry = dry_run
        self.log = log
        self.stats = SyncStats()
        self.seen_sources: set[str] = set()
        self.videos = load_settings().get("download_videos", False)

    # ------------------------------------------------------------ 入口

    def run(self) -> SyncStats:
        state = self.c.course_state(self.cid)
        layout = plan_layout(state)
        cms = {str(cm["id"]): cm for cm in state["cm"]}
        sections = {s["number"]: s for s in state["section"]}

        for num in sorted(sections):
            for cmid in sections[num].get("cmlist") or []:
                cm = cms.get(str(cmid))
                if not cm or cm.get("uservisible") is False:
                    continue
                folder = layout.folders.get(cm.get("sectionnumber", num), "General")
                try:
                    self.handle_cm(cm, folder, layout.titles.get(num, ""))
                except Exception as e:  # noqa: BLE001  一个坏活动不该拖垮整门课
                    if e.__class__.__name__ == "MoodleAuthError":
                        raise
                    self.stats.errors.append(f"{cm.get('name')} ({cm['id']}): {e}")
                    self.log(f"  ! {cm.get('name')}: {e}")

        # label、CMS 块和每个活动的说明文字只在课程页 HTML 里
        self.scan_section_pages(state, layout, cms)
        if not self.dry:
            self.con.execute("UPDATE courses SET synced_at=? WHERE id=?", (now_iso(), self.cid))
            self.con.commit()
        return self.stats

    # ------------------------------------------------------------ 各类活动

    def handle_cm(self, cm: dict, folder: str, section_title: str) -> None:
        mod = cm.get("module")
        url = cm.get("url")
        if mod in SKIP_MODULES or not url:
            return
        if mod == "resource":
            local, external, resp = self.c.resolve(url + "&redirect=1")
            if external is None and resp.status == 200 and "text/html" in (resp.headers.get("Content-Type") or ""):
                # 设成"嵌入显示"的资源不跳转，文件链接在页面里
                atts = [h for h, _ in collect_links(resp.text()).all if is_attachment(h)]
                for h in atts[:1]:
                    self.fetch_file(h, folder, cm, section_title)
                return
            self.fetch_file(local, folder, cm, section_title, resolved=True)
        elif mod == "folder":
            page = self.c.html(url)
            sub = f"{folder}/{safe_name(cm['name'])}"
            for h, _ in collect_links(page).all:
                if "/mod_folder/content/" in h:
                    self.fetch_file(h, sub, cm, section_title)
        elif mod == "url":
            local, external, _ = self.c.resolve(url + "&redirect=1")
            target = external or local
            if urllib.parse.urlsplit(target).hostname == HOST and "/pluginfile.php/" in target:
                self.fetch_file(target, folder, cm, section_title)
            else:
                self.add_link(folder, section_title, clean(cm["name"]), target)
        elif mod in ("assign", "page", "book"):
            # 作业说明、页面里挂的附件（spec PDF、starter code）才是要的东西
            page = self.c.html(url)
            for h, _ in collect_links(page).all:
                if is_attachment(h) and f"/{self._context_hint(mod)}" in h:
                    self.fetch_file(h, folder, cm, section_title)
            self.add_link(folder, section_title, clean(cm["name"]), url)

    @staticmethod
    def _context_hint(mod: str) -> str:
        return {"assign": "mod_assign/", "page": "mod_page/", "book": "mod_book/"}[mod]

    def scan_section_pages(self, state: dict, layout: Layout, cms: dict) -> None:
        pages: dict[str, None] = {}
        for sec in state["section"]:
            if not sec.get("cmlist") or not sec.get("sectionurl"):
                continue
            pages[sec["sectionurl"].split("#", 1)[0]] = None
        for page_url in pages:
            try:
                found = collect_links(self.c.html(page_url)).by_activity
            except Exception as e:  # noqa: BLE001
                if e.__class__.__name__ == "MoodleAuthError":
                    raise
                self.stats.errors.append(f"{page_url}: {e}")
                continue
            for cmid, links in found.items():
                cm = cms.get(cmid)
                if not cm:
                    continue
                num = cm.get("sectionnumber")
                folder = layout.folders.get(num, "General")
                title = layout.titles.get(num, "")
                for href, text in links:
                    if is_attachment(href):
                        try:
                            self.fetch_file(href, folder, cm, title)
                        except Exception as e:  # noqa: BLE001
                            if e.__class__.__name__ == "MoodleAuthError":
                                raise
                            self.stats.errors.append(f"{href}: {e}")
                    elif is_external(href):
                        label = text or clean(cm.get("name"))
                        self.add_link(folder, title, label, href)

    # ------------------------------------------------------------ 落盘

    def fetch_file(self, url: str, folder: str, cm: dict, section_title: str, resolved: bool = False) -> None:
        # pluginfile 地址本身就带 revision，不用再请求一次；其它的要跟一下跳转才知道
        if resolved or "/pluginfile.php/" in url:
            source = url.split("?", 1)[0]
        else:
            source = self.c.resolve(url)[0].split("?", 1)[0]
        if source in self.seen_sources:
            return
        self.seen_sources.add(source)
        row = self.con.execute("SELECT path FROM files WHERE course_id=? AND source=?",
                               (self.cid, source)).fetchone()
        if row and (FILES_DIR / row["path"]).exists():
            self.stats.skipped += 1
            return
        guess = urllib.parse.unquote(urllib.parse.urlsplit(source).path.rsplit("/", 1)[-1])
        if not self.videos and guess.lower().endswith(VIDEO_EXT):
            # 默认不下录像：记成链接，在 links.md 里能点开看（monash config download_videos true 改）
            self.add_link(folder, section_title, f"🎬 {clean(cm.get('name')) or guess}", source)
            return
        rel_dir = f"{self.root}/{folder}"
        if self.dry:
            self.log(f"  + {rel_dir}/{safe_name(guess)}")
            self.stats.downloaded += 1
            return
        tmp = FILES_DIR / rel_dir / f".dl-{cm['id']}.part"
        resp = self.c.download(source, tmp)
        name = safe_name(filename_from(resp, guess) or guess)
        rel = self._unique(f"{rel_dir}/{name}", source)
        dest = FILES_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
        size = dest.stat().st_size
        self.con.execute(
            "INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(course_id, source) DO UPDATE SET"
            " path=excluded.path, size=excluded.size, synced_at=excluded.synced_at",
            (self.cid, source, int(cm["id"]), rel, size, section_title, clean(cm.get("name")), now_iso()))
        self.con.commit()
        self.stats.downloaded += 1
        self.stats.bytes += size
        self.log(f"  ↓ {rel} ({size // 1024} KB)")

    def _unique(self, rel: str, source: str) -> str:
        """同名文件换了内容（revision 变了）就覆盖旧的；别的来源撞名才加序号。"""
        stem, dot, ext = rel.rpartition(".")
        if not dot or "/" in ext:
            stem, ext = rel, ""
        n, cand = 1, rel
        while True:
            owner = self.con.execute("SELECT source FROM files WHERE course_id=? AND path=?",
                                     (self.cid, cand)).fetchone()
            if owner is None or owner["source"] == source or _same_file(owner["source"], source):
                if owner is not None and owner["source"] != source:
                    self.con.execute("DELETE FROM files WHERE course_id=? AND source=?",
                                     (self.cid, owner["source"]))
                return cand
            n += 1
            cand = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"

    def add_link(self, folder: str, section: str, title: str, url: str) -> None:
        if self.dry:
            return
        cur = self.con.execute(
            "INSERT OR IGNORE INTO links (course_id, folder, section, title, url, synced_at)"
            " VALUES (?,?,?,?,?,?)", (self.cid, folder, section, title, url, now_iso()))
        self.stats.links += cur.rowcount
        self.con.commit()


def _same_file(a: str, b: str) -> bool:
    """pluginfile 地址去掉 revision 那一段后相同，就是同一个文件的新版本。"""
    strip = lambda u: re.sub(r"/content/\d+/", "/content/X/", urllib.parse.urlsplit(u).path)  # noqa: E731
    return strip(a) == strip(b)


IGNORED_HOSTS = ("okta.com", "monash.edu/students", "google.com/url")


def is_external(href: str) -> bool:
    parts = urllib.parse.urlsplit(href)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.hostname == HOST:
        return False
    return not any(h in href for h in IGNORED_HOSTS)


def write_links_md(con, course_id: int, root: str) -> None:
    """每个目录一份 links.md，从数据库重写（外部链接没法下载，就汇总成清单）。"""
    rows = con.execute("SELECT folder, section, title, url FROM links WHERE course_id=?"
                       " ORDER BY folder, section, rowid", (course_id,)).fetchall()
    by_folder: dict[str, list] = {}
    for r in rows:
        by_folder.setdefault(r["folder"], []).append(r)
    for folder, items in by_folder.items():
        lines = [f"# {folder} 的链接", ""]
        last = None
        seen = set()
        for r in items:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            if r["section"] != last and r["section"]:
                lines += ["", f"## {r['section']}", ""]
                last = r["section"]
            lines.append(f"- [{r['title'] or r['url']}]({r['url']})")
        path = FILES_DIR / root / folder / "links.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).replace("\n\n\n", "\n\n") + "\n", encoding="utf-8")
