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

import hashlib
import html
import json
import os
import posixpath
import re
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field

from . import htmlmd, moodle_content
from .moodlelib import (BASE_URL, FILES_DIR, HOST, MoodleClient, collect_links, filename_from,
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
# 课程页上链到本站这几类活动的链接，跟进去把内容存下来，而不是只记一个链接
MOD_LINK_RE = re.compile(r"/mod/(resource|page|folder|book)/view\.php")
FOLDER_REFRESH_DAYS = 7          # 文件夹整包下载最多每 7 天重下一次（Moodle 不给文件夹的修改时间）
ASSETS = "assets"                # 页面里的图片放在课程目录的 assets/，按内容哈希命名，相同的只存一份


def clean(text: str) -> str:
    return " ".join(html.unescape(text or "").split())


NAME_LIMIT = int(load_settings().get("name_limit") or 80)


def safe_name(name: str, limit: int | None = None) -> str:
    """能当文件/目录名用。去掉路径分隔符和 Windows 不认的字符，截短但保留扩展名。

    默认上限 80（设置里的 name_limit）：Windows 默认整条路径不能超过 260 个字符，
    课程/周次/文件夹/文件四层叠起来很容易超。只在 Linux 服务器上跑的部署可以放宽，
    但已经同步过的库别改，改了文件名会变，旧文件会被当成另一份再下一遍。
    """
    limit = limit or NAME_LIMIT
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
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    failed_cmids: list[int] = field(default_factory=list)
    bytes: int = 0


class CourseSync:
    def __init__(self, client: MoodleClient, con, course: dict, *, dry_run: bool = False, log=print, only_cmids: set[int] | None = None):
        self.only_cmids = only_cmids
        self.c = client
        self.con = con
        self.course = course
        self.cid = int(course["id"])
        self.root = course_folder(course)
        self.dry = dry_run
        self.log = log
        self.stats = SyncStats()
        self.seen_sources: set[str] = set()
        self.done_cms: set[str] = set()
        self.seen_links: set[tuple[str, str]] = set()
        self.cms: dict = {}
        self.videos = load_settings().get("download_videos", False)

    # ------------------------------------------------------------ 入口

    def run(self) -> SyncStats:
        state = self.c.course_state(self.cid)
        layout = plan_layout(state)
        cms = {str(cm["id"]): cm for cm in state["cm"]}
        self.cms = cms
        sections = {s["number"]: s for s in state["section"]}
        if not self.dry and self.only_cmids is None:
            self.clean_partials()
            # 说明表每轮重写（Google 导出的记录由 google_export 自己管）
            self.con.execute("DELETE FROM notes WHERE course_id=? AND kind NOT LIKE 'google%'", (self.cid,))

        for num in sorted(sections):
            for cmid in sections[num].get("cmlist") or []:
                cm = cms.get(str(cmid))
                if not cm or cm.get("uservisible") is False or (self.only_cmids is not None and int(cm["id"]) not in self.only_cmids):
                    continue
                folder = layout.folders.get(cm.get("sectionnumber", num), "General")
                try:
                    self.handle_cm(cm, folder, layout.titles.get(num, ""))
                except Exception as e:  # noqa: BLE001  一个坏活动不该拖垮整门课
                    if e.__class__.__name__ == "MoodleAuthError":
                        raise
                    self.stats.failed_cmids.append(int(cm["id"]))
                    self.stats.errors.append(f"{cm.get('name')} ({cm['id']}): {e}")
                    self.log(f"  ! {cm.get('name')}: {e}")

        # label、CMS 块和每个活动的说明文字只在课程页 HTML 里
        if self.only_cmids is None:
            self.scan_section_pages(state, layout, cms)
        if not self.dry and self.only_cmids is None:
            self.mark_removed()
            self.prune_links()
            self.con.execute("UPDATE courses SET synced_at=? WHERE id=?", (now_iso(), self.cid))
            self.con.commit()
        return self.stats

    def clean_partials(self) -> None:
        """清掉以前中断的下载留下的临时文件（.dl-*，1.0.3 及以前还会是 .dl-*.part.part）。

        同步是一门一门串行跑的，跑到这门课时不会有正在下的文件。
        """
        root = FILES_DIR / self.root
        if root.exists():
            for p in root.rglob(".dl-*"):
                if p.is_file():
                    p.unlink(missing_ok=True)

    def prune_links(self) -> None:
        """这一轮没再见到的链接删掉（Moodle 上删了，或者以前从整个网页收进来的导航链接）。
        和 mark_removed 一样，只在整门课没有错误时做。"""
        if self.stats.errors:
            return
        for r in self.con.execute("SELECT folder, url FROM links WHERE course_id=?", (self.cid,)).fetchall():
            if (r["folder"], r["url"]) not in self.seen_links:
                self.con.execute("DELETE FROM links WHERE course_id=? AND folder=? AND url=?", (self.cid, r["folder"], r["url"]))
        self.con.commit()

    def mark_removed(self) -> None:
        """这一轮没再见到的文件标成已移除（老师下架了，或者换了个文件名重新上传）。

        只有整门课一个错误都没有时才标：有活动请求失败的话，没见到不代表没了。
        只管同步器自己下的（cmid 不为空），讲义网页和录播不归这里管。文件本身不删。
        """
        if self.stats.errors:
            return
        rows = self.con.execute("SELECT source, path, removed_at FROM files WHERE course_id=? AND cmid IS NOT NULL",
                                (self.cid,)).fetchall()
        now = now_iso()
        for r in rows:
            seen = r["source"] in self.seen_sources
            # 视频的字幕稿（transcribe 登记成 transcript:<视频路径>）跟着视频一起标
            sources = (r["source"], f"transcript:{r['path']}")
            if seen and r["removed_at"]:
                self.con.execute("UPDATE files SET removed_at=NULL WHERE course_id=? AND source IN (?,?)",
                                 (self.cid, *sources))
            elif not seen and not r["removed_at"]:
                self.con.execute("UPDATE files SET removed_at=? WHERE course_id=? AND source IN (?,?)",
                                 (now, self.cid, *sources))
                self.stats.removed += 1
        self.con.commit()

    # ------------------------------------------------------------ 各类活动

    def handle_cm(self, cm: dict, folder: str, section_title: str) -> None:
        mod = cm.get("module")
        url = cm.get("url")
        if mod in SKIP_MODULES or not url or str(cm["id"]) in self.done_cms:
            return
        self.done_cms.add(str(cm["id"]))
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
            self.fetch_folder(cm, folder, section_title)
        elif mod == "url":
            local, external, _ = self.c.resolve(url + "&redirect=1")
            target = external or local
            inner = self.internal_cm(target, clean(cm.get("name")))
            if urllib.parse.urlsplit(target).hostname == HOST and "/pluginfile.php/" in target:
                self.fetch_file(target, folder, cm, section_title)
            elif inner is not None:
                # 指向本站 Page / 资源 / 文件夹 / Book 的链接：把那个活动的内容存下来
                self.handle_cm(inner, folder, section_title)
            else:
                self.add_link(folder, section_title, clean(cm["name"]), target)
        elif mod == "page":
            page = self.c.html(url)
            self.save_page(cm, folder, section_title, page)
            self._attachments(page, "page", folder, cm, section_title)
        elif mod == "book":
            # 打印视图一次给出全部章节；打开它 Moodle 只记“打印”，不算“查看”
            page = self.c.html(f"{BASE_URL}/mod/book/tool/print/index.php?id={cm['id']}")
            self.save_book(cm, folder, section_title, page)
            self._attachments(page, "book", folder, cm, section_title)
        elif mod == "assign":
            # 作业说明里挂的附件（spec PDF、starter code）+ 说明正文 + 学生能看到的评分细则
            page = self.c.html(url)
            self.save_assign(cm, folder, section_title, page, url)
            self._attachments(page, "assign", folder, cm, section_title)

    def _attachments(self, page: str, mod: str, folder: str, cm: dict, section_title: str) -> None:
        for h, _ in collect_links(page).all:
            if is_attachment(h) and f"/{self._context_hint(mod)}" in h:
                self.fetch_file(h, folder, cm, section_title)

    def content_links(self, node, folder: str, section_title: str) -> None:
        """正文（不是整个网页：导航、页脚的链接不要）里的外链记进 links.md，比如录播、阅读材料。"""
        if node is None:
            return
        for a in node.find_all("a"):
            href = urllib.parse.urljoin(BASE_URL, a.get("href") or "")
            if is_external(href):
                self.add_link(folder, section_title, " ".join(a.text().split()) or href, href)

    def internal_cm(self, href: str, text: str = "") -> dict | None:
        """本站 /mod/<page|resource|folder|book>/view.php?id=N 链接对应的活动。

        课程结构里有就用它；没有（“隐身”活动：不在课程页上、只被别处链接）就按链接现做一个。"""
        parts = urllib.parse.urlsplit(href or "")
        m = MOD_LINK_RE.search(parts.path)
        if parts.hostname != HOST or not m:
            return None
        cmid = (urllib.parse.parse_qs(parts.query).get("id") or [""])[0]
        if not cmid.isdigit():
            return None
        return self.cms.get(cmid) or {"id": int(cmid), "module": m.group(1), "name": text or f"{m.group(1)} {cmid}",
                                      "url": f"{BASE_URL}/mod/{m.group(1)}/view.php?id={cmid}"}

    # ------------------------------------------------------------ 正文存成 Markdown

    def save_markdown(self, folder: str, name: str, md: str, cm: dict, section_title: str, kind: str) -> None:
        source = f"md:{kind}:{cm['id']}"
        self.seen_sources.add(source)
        rel = f"{self.root}/{folder}/{safe_name(name)}.md"
        if self.dry:
            self.log(f"  + {rel}")
            return
        dest = FILES_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_text(encoding="utf-8") != md:
            dest.write_text(md, encoding="utf-8")
            self.stats.downloaded += 1
            self.log(f"  ✎ {rel}")
        else:
            self.stats.skipped += 1
        self.con.execute(
            "INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(course_id, source) DO UPDATE SET"
            " path=excluded.path, size=excluded.size, synced_at=excluded.synced_at, removed_at=NULL",
            (self.cid, source, int(cm["id"]), rel, dest.stat().st_size, section_title, clean(cm.get("name")), now_iso()))
        self.con.commit()

    def _md(self, node, folder: str, cm: dict) -> str:
        md_dir = f"{self.root}/{folder}"
        return htmlmd.to_markdown(node, BASE_URL, on_image=lambda u: self.asset(u, md_dir, cm))

    def save_page(self, cm: dict, folder: str, section_title: str, page: str) -> None:
        body = moodle_content.page_body(page)
        name = clean(cm.get("name"))
        if body is None:
            self.add_note("page_empty", folder, name, cm["url"], "页面上没找到正文")
            return
        md = f"# {name}\n\n来源：{cm['url']}\n\n" + self._md(body, folder, cm)
        self.save_markdown(folder, name, md, cm, section_title, "page")
        self.content_links(body, folder, section_title)

    def save_book(self, cm: dict, folder: str, section_title: str, page: str) -> None:
        name = clean(cm.get("name"))
        chapters = moodle_content.book_chapters(page)
        if not chapters:
            self.add_note("book_empty", folder, name, cm["url"], "Book 打印视图里没有章节")
            return
        md = f"# {name}\n\n来源：{cm['url']}\n"
        for title, node in chapters:
            md += f"\n## {title}\n\n" + self._md(node, folder, cm)
            self.content_links(node, folder, section_title)
        self.save_markdown(folder, name, md, cm, section_title, "book")

    def save_assign(self, cm: dict, folder: str, section_title: str, page: str, url: str) -> None:
        name = clean(cm.get("name"))
        intro = moodle_content.assign_intro(page)
        grading, kind = moodle_content.grading_markdown(page)
        md = f"# {name}\n\n来源：{url}\n\n"
        md += self._md(intro, folder, cm) if intro is not None else "（Moodle 上没有作业说明正文）\n"
        self.content_links(intro, folder, section_title)
        if grading:
            md += "\n## " + ("评分细则（Rubric）" if kind == "rubric" else "评分指南（Marking guide）") + "\n\n" + grading
        else:
            self.add_note("grading_hidden", folder, name, url,
                          "作业页面上没有给学生看评分细则（可能没设、只在打分后显示，或在说明附件里）")
        self.save_markdown(folder, name, md, cm, section_title, "assign")

    def asset(self, url: str, md_dir: str, cm: dict) -> str:
        """页面里的本站图片下载到 课程/assets/<内容哈希>.<扩展名>，返回相对 md_dir 的路径。
        站外图片和下载失败的原样返回（导出的索引里会列出远程图片）；装饰图片返回 ""，不导出。"""
        parts = urllib.parse.urlsplit(url)
        if is_decorative(url):
            return ""
        if parts.hostname != HOST or "/pluginfile.php/" not in parts.path or self.dry:
            return url
        source = url.split("?", 1)[0]
        self.seen_sources.add(source)
        row = self.con.execute("SELECT path FROM files WHERE course_id=? AND source=?", (self.cid, source)).fetchone()
        if not (row and (FILES_DIR / row["path"]).exists()):
            tmp = FILES_DIR / self.root / ASSETS / f".dl-asset-{os.getpid()}"
            try:
                self.c.download(source, tmp)
            except Exception as e:  # noqa: BLE001
                if e.__class__.__name__ == "MoodleAuthError":
                    raise
                self.add_note("image_failed", md_dir.split("/", 1)[-1], posixpath.basename(parts.path), source, f"图片下载失败：{e}")
                tmp.unlink(missing_ok=True)
                return url
            ext = os.path.splitext(urllib.parse.unquote(parts.path))[1][:10]
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:16]
            asset_dir = FILES_DIR / self.root / ASSETS
            existing = next(iter(sorted(asset_dir.glob(digest + ".*"))), None)
            rel = existing.relative_to(FILES_DIR).as_posix() if existing else f"{self.root}/{ASSETS}/{digest}{ext}"
            if (FILES_DIR / rel).exists():
                tmp.unlink()
            else:
                tmp.replace(FILES_DIR / rel)
            self.con.execute(
                "INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(course_id, source) DO UPDATE SET path=excluded.path, synced_at=excluded.synced_at,"
                " removed_at=NULL",
                (self.cid, source, int(cm["id"]), rel, (FILES_DIR / rel).stat().st_size, ASSETS,
                 posixpath.basename(parts.path), now_iso()))
            self.con.commit()
            path = rel
        else:
            path = row["path"]
        return os.path.relpath(FILES_DIR / path, FILES_DIR / md_dir).replace(os.sep, "/")

    def add_note(self, kind: str, folder: str, title: str, url: str, reason: str) -> None:
        if self.dry:
            return
        self.con.execute("INSERT OR REPLACE INTO notes (course_id, kind, folder, title, url, reason, synced_at)"
                         " VALUES (?,?,?,?,?,?,?)", (self.cid, kind, folder, title, url, reason, now_iso()))
        self.con.commit()

    # ------------------------------------------------------------ 文件夹

    def fetch_folder(self, cm: dict, folder: str, section_title: str) -> None:
        """整个文件夹用 download_folder.php 打成 zip 下下来，按原来的子目录和原文件名放好。

        这个地址打开不算“查看”（view.php 才算）。Moodle 不给文件夹的修改时间，所以最多每
        FOLDER_REFRESH_DAYS 天重下一次；文件夹关了“下载”按钮的，退回去一个个下。"""
        sub = f"{folder}/{safe_name(cm['name'])}"
        prefix = f"folder:{cm['id']}:"
        rows = self.con.execute("SELECT source, path, synced_at FROM files WHERE course_id=? AND source LIKE ?",
                                (self.cid, prefix.replace("%", r"\%") + "%")).fetchall()
        fresh = rows and all((FILES_DIR / r["path"]).exists() for r in rows) and \
            time.time() - max(_ts(r["synced_at"]) for r in rows) < FOLDER_REFRESH_DAYS * 86400
        if fresh:
            self.seen_sources.update(r["source"] for r in rows)
            self.stats.skipped += len(rows)
            self.check_notebooks([r["path"] for r in rows], sub)
            return
        if self.dry:
            self.log(f"  + {self.root}/{sub}/（整个文件夹）")
            return
        tmp = FILES_DIR / self.root / folder / f".dl-folder-{cm['id']}"
        try:
            self.c.download(f"{BASE_URL}/mod/folder/download_folder.php?id={cm['id']}", tmp)
            ok = zipfile.is_zipfile(tmp)
        except Exception as e:  # noqa: BLE001
            if e.__class__.__name__ == "MoodleAuthError":
                raise
            ok = False
        if not ok:
            tmp.unlink(missing_ok=True)
            self._folder_by_page(cm, sub, section_title)
            return
        with zipfile.ZipFile(tmp) as z:
            members = [i for i in z.infolist() if not i.is_dir()]
            names = [_zip_parts(i.filename) for i in members]
            # 有的 zip 外面包了一层和文件夹同名的目录，去掉
            if names and all(len(n) > 1 and n[0] == safe_name(cm["name"]) for n in names):
                names = [n[1:] for n in names]
            written = []
            for info, parts in zip(members, names):
                if not parts:
                    continue
                rel = f"{self.root}/{sub}/" + "/".join(parts)
                dest = FILES_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    while chunk := src.read(1 << 16):
                        out.write(chunk)
                source = prefix + "/".join(parts)
                self.seen_sources.add(source)
                self.con.execute(
                    "INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at)"
                    " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(course_id, source) DO UPDATE SET"
                    " path=excluded.path, size=excluded.size, synced_at=excluded.synced_at, removed_at=NULL",
                    (self.cid, source, int(cm["id"]), rel, dest.stat().st_size, section_title,
                     clean(cm.get("name")), now_iso()))
                self.stats.downloaded += 1
                self.stats.bytes += dest.stat().st_size
                written.append(rel)
        tmp.unlink(missing_ok=True)
        self.con.commit()
        self.log(f"  ↓ {self.root}/{sub}/（{len(written)} 个文件）")
        self.check_notebooks(written, sub)

    def _folder_by_page(self, cm: dict, sub: str, section_title: str) -> None:
        """退路：打开文件夹页面（会记一次“查看”），一个个下载，保留 content/<rev>/ 后面的子目录。"""
        page = self.c.html(cm["url"])
        for h, _ in collect_links(page).all:
            m = re.search(r"/mod_folder/content/\d+/(.+)$", urllib.parse.urlsplit(h).path)
            if not m:
                continue
            inner = posixpath.dirname(urllib.parse.unquote(m.group(1)))
            target = sub + ("/" + "/".join(_zip_parts(inner)) if inner else "")
            self.fetch_file(h, target, cm, section_title)
        rows = self.con.execute("SELECT path FROM files WHERE course_id=? AND cmid=?",
                                (self.cid, cm["id"])).fetchall()
        self.check_notebooks([r["path"] for r in rows], sub)

    def check_notebooks(self, rels: list[str], folder: str) -> None:
        """Notebook 里引用的本地图片（images/x.PNG）要在对应位置；找不到、或者只是大小写对不上，记下来。"""
        for rel in rels:
            if not rel.lower().endswith(".ipynb"):
                continue
            nb = FILES_DIR / rel
            try:
                cells = json.loads(nb.read_text(encoding="utf-8")).get("cells", [])
            except (OSError, ValueError):
                continue
            text = "\n".join("".join(c.get("source") or []) if isinstance(c.get("source"), list) else str(c.get("source"))
                             for c in cells)
            refs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text) + re.findall(r"<img[^>]+src=[\"']([^\"']+)", text)
            for ref in dict.fromkeys(refs):
                if re.match(r"^[a-z]+:", ref) or ref.startswith("/"):
                    continue
                target = nb.parent / urllib.parse.unquote(ref)
                if target.is_file() and any(p.name == target.name for p in target.parent.iterdir()):
                    continue
                twin = next((p for p in target.parent.glob("*") if p.name.lower() == target.name.lower()), None) \
                    if target.parent.is_dir() else None
                reason = (f"Notebook 引用 {ref}，Moodle 上的文件名是 {twin.name}（只差大小写，区分大小写的系统上会显示不出来）"
                          if twin else f"Notebook 引用的 {ref} 不在 Moodle 的文件夹里")
                self.add_note("notebook_image", folder, nb.name, f"{rel}#{ref}", reason)

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
                    inner = None if is_attachment(href) else self.internal_cm(href, clean(text))
                    if is_attachment(href) or inner is not None:
                        try:
                            if inner is not None:
                                # 标签和说明里链到本站 Page / 资源 / 文件夹 / Book：存内容，不记链接
                                self.handle_cm(inner, folder, title)
                            else:
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
        tmp = FILES_DIR / rel_dir / f".dl-{cm['id']}"   # 下载时 client.download 自己会加 .part
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
        """同名文件换了内容（revision 变了）或者旧的已经下架，就覆盖旧的；别的来源撞名才加序号。"""
        stem, dot, ext = rel.rpartition(".")
        if not dot or "/" in ext:
            stem, ext = rel, ""
        n, cand = 1, rel
        while True:
            owner = self.con.execute("SELECT source, removed_at FROM files WHERE course_id=? AND path=?",
                                     (self.cid, cand)).fetchone()
            # 已经下架的旧文件占着这个名字，也直接让给新文件
            if owner is None or owner["source"] == source or owner["removed_at"] or _same_file(owner["source"], source):
                if owner is not None and owner["source"] != source:
                    self.con.execute("DELETE FROM files WHERE course_id=? AND source=?",
                                     (self.cid, owner["source"]))
                return cand
            n += 1
            cand = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"

    def add_link(self, folder: str, section: str, title: str, url: str) -> None:
        if self.dry:
            return
        self.seen_links.add((folder, url))
        cur = self.con.execute(
            "INSERT OR IGNORE INTO links (course_id, folder, section, title, url, synced_at)"
            " VALUES (?,?,?,?,?,?)", (self.cid, folder, section, title, url, now_iso()))
        self.stats.links += cur.rowcount
        self.con.commit()


# 不是课程内容的图片：Moodle 主题的文件类型小图标、录播平台的视频缩略图（要另外登录，也没有信息量）
DECORATIVE_HOSTS = ("panopto.com", "panopto.eu", "ytimg.com", "vimeocdn.com")


def is_decorative(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == HOST and parts.path.startswith(("/theme/", "/pix/")):
        return True
    return any(host == h or host.endswith("." + h) for h in DECORATIVE_HOSTS)


def _ts(iso: str | None) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _zip_parts(name: str) -> list[str]:
    """zip 里的路径拆成安全的几段：去掉 .. 和开头的 /，每段只换掉文件系统不认的字符，不截短、不改大小写。"""
    out = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        part = re.sub(r'[\\:*?"<>|\x00-\x1f]', "_", part).strip(" ")
        if part:
            out.append(part)
    return out


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
    from .google_export import exported
    by_folder: dict[str, list] = {}
    for r in rows:
        by_folder.setdefault(r["folder"], [])
        if not exported(con, course_id, r["url"]):
            by_folder[r["folder"]].append(r)
    for folder, items in by_folder.items():
        if not items:
            (FILES_DIR / root / folder / "links.md").unlink(missing_ok=True)
            continue
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
