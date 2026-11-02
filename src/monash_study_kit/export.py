"""整门课的课程资料导出：Moodle 课件和 Ed Lessons 放进同一个目录（或 zip），附一份索引。

    FIT1043/
        README.md                     索引：导出了什么、哪些没导出以及原因
        Moodle/
            Week 01 - .../xxx.pdf
            Week 01 - .../links.md    这个目录里没法下载的外部链接
        Ed Lessons/
            03 Week 3 - .../02 Lesson/01 Slides.pdf
            03 Week 3 - .../02 Lesson/Links.md
            03 Week 3 - .../02 Lesson/Quiz.md

只用本机已经同步好的数据，不联网：先 monash sync，再导出。只导课程资料，不含
提交记录、成绩、反馈、邮件、私信和考勤。Zoom 链接里的会议密码会去掉。
CLI（monash export）和服务器上的整门课 zip 共用这里。
"""
from __future__ import annotations

import os
import re
import sqlite3
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import edexport, edquery
from .moodlelib import FILES_DIR

# 课件大多已经压过，再压一遍只费 CPU
STORED = edexport.STORED
# 链接里的这些参数是会议密码或一次性令牌，导出给别处看的资料里不该带
SECRET_PARAMS = {"pwd", "passcode", "password", "token", "tk"}


@dataclass
class Plan:
    code: str
    entries: list = field(default_factory=list)   # [(包内路径, Path 或 str 内容)]
    missing: list = field(default_factory=list)   # [{"where", "title", "url", "reason"}]
    notes: list = field(default_factory=list)     # 写进索引的说明
    moodle: dict | None = None
    ed: dict | None = None

    def add(self, arc: str, item) -> None:
        self.entries.append((arc, item))


def scrub_url(url: str) -> str:
    """去掉会议密码这类参数：https://monash.zoom.us/j/123?pwd=abc → https://monash.zoom.us/j/123"""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in SECRET_PARAMS]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(kept)))


def _code(ref: str) -> str:
    m = re.search(r"[A-Za-z]{3}\d{4}", ref or "")
    return m.group(0).upper() if m else (ref or "").strip()


# ---------------------------------------------------------------- Moodle

def moodle_course(con: sqlite3.Connection, code: str) -> dict | None:
    """库里课号对得上的 Moodle 课。同一门课有好几个学期时取跟踪中、id 最大（最新）的那个。"""
    rows = [dict(r) for r in con.execute("SELECT id, code, folder, fullname, tracked, synced_at FROM courses")]
    hits = [r for r in rows if (r["code"] or "").upper() == code]
    hits.sort(key=lambda r: (not r["tracked"], -r["id"]))
    return hits[0] if hits else None


def _links_md(folder: str, items: list) -> str:
    lines = [f"# {folder} 的链接", ""]
    last, seen = None, set()
    for r in items:
        url = scrub_url(r["url"])
        if url in seen:
            continue
        seen.add(url)
        if r["section"] and r["section"] != last:
            lines += ["", f"## {r['section']}", ""]
            last = r["section"]
        lines.append(f"- [{r['title'] or url}]({url})")
    return "\n".join(lines).replace("\n\n\n", "\n\n") + "\n"


def plan_moodle(p: Plan, con: sqlite3.Connection) -> None:
    c = moodle_course(con, p.code)
    if c is None:
        p.notes.append(f"Moodle：本机库里没有 {p.code}。先运行 monash courses 把它勾上，再 monash sync。")
        return
    root = c["folder"] or ""
    stats = {"id": c["id"], "name": c["fullname"], "files": 0, "bytes": 0, "links": 0, "synced_at": c["synced_at"]}
    if not c["synced_at"]:
        p.notes.append(f"Moodle：{p.code} 还没同步过（monash sync {p.code}）。")
    for r in con.execute("SELECT path, title, section FROM files WHERE course_id=? AND removed_at IS NULL "
                         "ORDER BY path", (c["id"],)):
        path = FILES_DIR / r["path"]
        rel = r["path"][len(root) + 1:] if root and r["path"].startswith(root + "/") else r["path"]
        if path.is_file() and FILES_DIR.resolve() in path.resolve().parents:
            p.add(f"{p.code}/Moodle/{rel}", path)
            stats["files"] += 1
            stats["bytes"] += path.stat().st_size
        else:
            p.missing.append({"where": f"Moodle/{os.path.dirname(rel)}", "title": r["title"] or rel,
                              "url": "", "reason": "本地文件不见了（重新 monash sync）"})
    by_folder: dict[str, list] = {}
    for r in con.execute("SELECT folder, section, title, url FROM links WHERE course_id=? ORDER BY folder, section, rowid",
                         (c["id"],)):
        by_folder.setdefault(r["folder"], []).append(r)
    for folder, items in by_folder.items():
        p.add(f"{p.code}/Moodle/{folder}/links.md", _links_md(folder, items))
        stats["links"] += len(items)
    p.moodle = stats


# ---------------------------------------------------------------- Ed

def _ed_reason(conn: sqlite3.Connection, code: str) -> str:
    """Ed 那边为什么没有 lessons：没找到课、没跟踪，还是课里本来就没有。"""
    try:
        c = edquery.resolve_course(conn, code)
    except edquery.NotFound:
        return (f"本机的 Ed 课程列表里没有 {code}：可能没加入这门课的 Ed，或者 Ed 上的课号不一样。"
                "monash ed courses 可以看有哪些课。")
    if not c.get("tracked"):
        return (f"Ed 上的 {c['code']} 没有被跟踪，所以 lessons 从来没同步过。"
                f"用 monash courses 把它勾上（或 monash sync 之后再看），然后重新导出。")
    return f"Ed 上的 {c['code']} 已跟踪，但还没有同步到 lessons（先 monash sync；Ed 上也可能本来就没有 lessons）。"


def plan_ed(p: Plan, conn: sqlite3.Connection) -> None:
    try:
        ep = edexport.plan(conn, p.code)
    except (LookupError, edquery.NotFound):
        p.notes.append("Ed Lessons：" + _ed_reason(conn, p.code))
        return
    base = f"{p.code}/Ed Lessons"
    cut = len(ep["root"]) + 1
    for arc, path in ep["files"]:
        p.add(f"{base}/{arc[cut:]}", path)
    for l in ep["lessons"]:
        folder = f"{base}/{l['folder'][cut:]}"
        md = [f"# {l['title']}", "", f"- Ed 原页面：{l['url']}"]
        md += [f"- [{t}]({scrub_url(u)})" for t, u in l["links"]]
        p.add(f"{folder}/Links.md", "\n".join(md) + "\n")
    # 按 Ed 原页面链接对应（lesson 标题可能重名）
    quiz_by_lesson = {q["url"]: q for q in edquery.quizzes(conn, ep["course_id"])}
    for l in ep["lessons"]:
        q = quiz_by_lesson.get(l["url"])
        if q:
            p.add(f"{base}/{l['folder'][cut:]}/Quiz.md", edquery.quiz_markdown([q]))
    for m in ep["missing"]:
        p.missing.append({"where": f"Ed Lessons/{m['lesson']}", "title": m["title"], "url": m["url"],
                          "reason": "上次同步没下载到（下载失败或超过大小上限）"})
    p.ed = {"id": ep["course_id"], "lessons": len(ep["lessons"]), "files": len(ep["files"]),
            "quizzes": len(quiz_by_lesson)}


# ---------------------------------------------------------------- 索引和写出

def _index(p: Plan) -> str:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    md = [f"# {p.code} 课程资料", "", f"导出于 {now}，来自本机已同步的 Moodle 和 Ed Lessons。"
          "这是个人学习副本，课件版权归课程团队，请不要再公开分发。", "",
          "不含：Ed 讨论帖、提交记录、成绩和反馈、邮件、私信、考勤。Zoom 等链接里的会议密码已去掉。", ""]
    if p.moodle:
        m = p.moodle
        md.append(f"- Moodle/：{m['files']} 个文件（{m['bytes'] / 1e6:.1f} MB），{m['links']} 条链接写在各目录的 links.md")
    if p.ed:
        e = p.ed
        md.append(f"- Ed Lessons/：{e['lessons']} 节 lesson，{e['files']} 个文件，{e['quizzes']} 节有 Quiz.md（只有题目，没有答案）")
    if p.notes:
        md += ["", "## 说明", ""] + [f"- {n}" for n in p.notes]
    if p.missing:
        md += ["", "## 没导出的内容", "", "| 位置 | 名称 | 原因 | 链接 |", "|---|---|---|---|"]
        for m in p.missing:
            cells = [m["where"], m["title"], m["reason"], scrub_url(m["url"]) if m["url"] else ""]
            md.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    return "\n".join(md) + "\n"


def plan(course: str, *, moodle_con: sqlite3.Connection | None, ed_conn: sqlite3.Connection | None) -> Plan:
    p = Plan(code=_code(course))
    if moodle_con is not None:
        plan_moodle(p, moodle_con)
    if ed_conn is not None:
        plan_ed(p, ed_conn)
    if not p.entries:
        raise LookupError(f"{p.code} 没有可导出的资料。\n" + "\n".join(p.notes))
    p.entries.insert(0, (f"{p.code}/README.md", _index(p)))
    return p


def write_zip(p: Plan, out) -> dict:
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for arc, item in p.entries:
            if isinstance(item, Path):
                z.write(item, arc, zipfile.ZIP_STORED if item.suffix.lower() in STORED else zipfile.ZIP_DEFLATED)
                total += item.stat().st_size
            else:
                z.writestr(arc, item)
    return _stats(p, total)


def write_dir(p: Plan, dest: Path) -> dict:
    """写成普通文件夹（已经存在的同名文件覆盖）。"""
    import shutil
    total = 0
    for arc, item in p.entries:
        target = dest / arc
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(item, Path):
            shutil.copy2(item, target)
            total += item.stat().st_size
        else:
            target.write_text(item, encoding="utf-8")
    return _stats(p, total)


def _stats(p: Plan, total: int) -> dict:
    return {"course": p.code, "entries": len(p.entries), "bytes": total, "moodle": p.moodle, "ed": p.ed,
            "missing": len(p.missing), "notes": p.notes}


def export(course: str, to: str | Path | None = None, *, as_dir: bool = False,
           moodle_con: sqlite3.Connection | None = None, ed_conn: sqlite3.Connection | None = None) -> dict:
    """导出到文件夹（默认“下载”）。as_dir=True 写成文件夹，否则一个 zip。"""
    p = plan(course, moodle_con=moodle_con, ed_conn=ed_conn)
    dest_dir = Path(to).expanduser() if to else edexport.default_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if as_dir:
        return {"path": str(dest_dir / p.code), **write_dir(p, dest_dir)}
    dest = dest_dir / f"{p.code} 课程资料 {datetime.now().strftime('%Y-%m-%d')}.zip"
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "wb") as f:
            stats = write_zip(p, f)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(dest), **stats}
