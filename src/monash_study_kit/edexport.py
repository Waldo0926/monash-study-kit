"""把一门课已经同步下来的 Ed Lessons 课件打成一个 zip（和 Ed Web 的“一键下载资料”一样按周分好）。

只打包本地已有的文件，不联网：先 monash sync，再导出。zip 里：

    FIT2109 Ed Lessons/
        README.md                              每节 lesson 的进度、Ed 链接，没下载到的文件
        03 Week 3 - Editor-Modern IDE/
            12 W3 Workshop/
                01 Workshop 3 Slides.pdf
                Links.md                       这节的阅读网页和 Ed 原页面链接

目录名从 ed.db 里的模块和 lesson 重新算，所以老版本按数字 id 存的文件也能打成同样的结构。
CLI（monash ed export）、本机课件页（monash library）和 MCP 共用这里。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import edquery
from .lessons import course_folder, ensure_lessons_schema, lesson_folder, module_folders
from .paths import ED_FILES_DIR

STORED = {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".gz", ".docx", ".pptx", ".xlsx"}
STATUS = {"completed": "已完成", "attempted": "做了一部分", "unattempted": "没开始"}
CHALLENGE_REASON = "Code challenge 右侧的工作区文件只能连接 Ed 工作区才读得到（会在 Ed 上创建工作区），为保持只读没有导出；题目说明已存成 .md"


def _lessons(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    rows = conn.execute('SELECT id, module_id, idx AS "index", title, status FROM lessons '
                        "WHERE course_id=? ORDER BY idx IS NULL, idx, id", (course_id,)).fetchall()
    return [dict(r) for r in rows]


def plan(conn: sqlite3.Connection, course, module: str | None = None) -> dict:
    """要打包的内容：[(zip 里的路径, 本地文件)]、每节的链接和没下载到的文件。"""
    ensure_lessons_schema(conn)
    c = edquery.resolve_course(conn, course)
    code = course_folder(c["code"], c["id"])
    lessons = _lessons(conn, c["id"])
    if not lessons:
        raise LookupError(f"{code} 还没有同步到 Ed Lessons：先运行 monash sync")
    modules = [dict(r) for r in conn.execute("SELECT id, name FROM modules WHERE course_id=?", (c["id"],))]
    names = {m["id"]: m["name"] for m in modules}
    mdirs = module_folders(modules, lessons)
    root = f"{code} Ed Lessons"
    out = {"course": code, "course_id": c["id"], "root": root, "files": [], "lessons": [], "missing": []}
    for pos, l in enumerate(lessons):
        mname = names.get(l["module_id"]) or ""
        if module and module.lower() not in mname.lower() and str(module) != str(l["module_id"]):
            continue
        folder = f"{root}/{mdirs.get(l['module_id'], mdirs[None])}/{lesson_folder(l, pos)}"
        url = f"https://edstem.org/au/courses/{c['id']}/lessons/{l['id']}"
        links, nfiles = [], 0
        for f in conn.execute("SELECT type, title, url, local_path FROM lesson_files WHERE lesson_id=? "
                              "ORDER BY COALESCE(idx, 1e9), id", (l["id"],)):
            path = ED_FILES_DIR / f["local_path"] if f["local_path"] else None
            if path is not None and path.is_file() and ED_FILES_DIR.resolve() in path.resolve().parents:
                out["files"].append((f"{folder}/{path.name}", path))
                nfiles += 1
            elif f["type"] == "webpage" and f["url"]:
                links.append((f["title"] or f["url"], f["url"]))
            elif f["type"] in ("pdf", "attachment"):
                out["missing"].append({"lesson": l["title"], "title": f["title"], "url": url})
            elif f["type"] == "challenge":
                out["missing"].append({"lesson": l["title"], "title": f["title"] or "Code challenge", "url": url,
                                       "reason": CHALLENGE_REASON})
        out["lessons"].append({"module": mname, "title": l["title"], "status": l["status"], "url": url,
                               "folder": folder, "files": nfiles, "links": links})
    if module and not out["lessons"]:
        raise LookupError(f"{code} 里没有模块名包含 “{module}” 的 lesson")
    assets = ED_FILES_DIR / code / "assets"
    if assets.is_dir():
        for path in sorted(assets.iterdir()):
            if path.is_file() and not path.name.endswith((".part", ".download")) and ED_FILES_DIR.resolve() in path.resolve().parents:
                out["files"].append((f"{root}/assets/{path.name}", path))
    return out


def _readme(p: dict) -> str:
    md = [f"# {p['course']} · Ed Lessons", "",
          f"从 Ed Lessons 同步下来的课件，导出于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}。"
          "这是你自己的学习副本，课件版权归课程团队，请不要再公开分发。", ""]
    module = None
    for l in p["lessons"]:
        if l["module"] != module:
            module = l["module"]
            md += [f"## {module or 'Other'}", ""]
        extra = "，".join(x for x in (f"{l['files']} 个文件" if l["files"] else "",
                                      f"{len(l['links'])} 个链接" if l["links"] else "") if x) or "只有视频或测验"
        md.append(f"- [{l['title']}]({l['url']}) · {STATUS.get(l['status'], l['status'] or '')} · {extra}")
    if p["missing"]:
        md += ["", "## 没下载到的文件", "", "下面这些上次同步时没下载成功（或超过大小上限），可以到 Ed 原页面看：", ""]
        md += [f"- {m['lesson']} · {m['title']}：{m['url']}" + (f"（{m['reason']}）" if m.get("reason") else "")
               for m in p["missing"]]
    return "\n".join(md) + "\n"


def _links(l: dict) -> str:
    md = [f"# {l['title']}", "", f"- Ed 原页面：{l['url']}"]
    md += [f"- [{title}]({url})" for title, url in l["links"]]
    return "\n".join(md) + "\n"


def write_zip(p: dict, out: BinaryIO) -> dict:
    """按 plan 写 zip 到 out（文件或临时文件），返回统计。"""
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{p['root']}/README.md", _readme(p))
        for l in p["lessons"]:
            if l["links"]:
                z.writestr(f"{l['folder']}/Links.md", _links(l))
        for arc, path in p["files"]:
            z.write(path, arc, zipfile.ZIP_STORED if path.suffix.lower() in STORED else zipfile.ZIP_DEFLATED)
            total += path.stat().st_size
    return {"course": p["course"], "files": len(p["files"]), "bytes": total,
            "lessons": len(p["lessons"]), "missing": len(p["missing"])}


def _windows_downloads() -> Path | None:
    """Windows 的“下载”可能被挪到 D 盘或 OneDrive，按系统登记的位置找。"""
    try:
        import ctypes
        from ctypes import wintypes
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = UUID("374DE290-123F-4565-9164-39C4925E467B")  # FOLDERID_Downloads
        guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except (AttributeError, OSError, ValueError):
        return None


def default_dir() -> Path:
    candidates = [_windows_downloads()] if sys.platform == "win32" else []
    candidates.append(Path.home() / "Downloads")
    for d in candidates:
        if d and d.is_dir():
            return d
    return Path.cwd()


def zip_name(p: dict, module: str | None = None) -> str:
    tag = f" ({module.strip()})" if module else ""
    name = f"{p['root']}{tag} {datetime.now().strftime('%Y-%m-%d')}.zip"
    return "".join("-" if ch in '\\/:*?"<>|' else ch for ch in name)


def export(conn: sqlite3.Connection, course, to: str | Path | None = None, module: str | None = None) -> dict:
    """导出到文件夹（默认“下载”），返回 {path, files, bytes, lessons, missing}。同一天再导一次会覆盖。"""
    p = plan(conn, course, module)
    if not p["files"] and not any(l["links"] for l in p["lessons"]):
        raise LookupError(f"{p['course']} 的 Ed Lessons 里还没有下载好的课件：先运行 monash sync")
    dest_dir = Path(to).expanduser() if to else default_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / zip_name(p, module)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "wb") as f:
            stats = write_zip(p, f)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(dest), **stats}
