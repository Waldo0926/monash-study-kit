"""按设置没下载的文件：列出来，挑几个单独补下。

同步时太大的、排除类型的、录像都记在 moodle.db 的 skipped 表里（见 syncer._record_skip）。
编号就是表的 rowid，同步用 upsert 更新，所以两次同步之间编号不变，可以放心照着 monash skipped 的输出输。

想一次补很多的话不用这里：调大上限再 monash sync 一次就行，已经下好的文件不会重下。
"""
from __future__ import annotations

from . import syncer
from .syncer import CourseSync, _mb, course_folder, safe_name, write_links_md

REASONS = {"size": "太大", "type": "排除的类型", "video": "录像"}


def _course_names(con) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in con.execute("SELECT id, code, fullname FROM courses")}


def display_path(row) -> str:
    """相对课程目录的位置，zip 里的文件带上文件夹名。"""
    if row["member"]:
        return f"{row['folder']}/{safe_name(row['cm_name'] or '')}/{row['name']}"
    return f"{row['folder']}/{row['name']}"


def list_skipped(con, course_id: int | None = None, include_videos: bool = True) -> list[dict]:
    q = "SELECT rowid AS id, * FROM skipped"
    where, params = [], []
    if course_id is not None:
        where.append("course_id=?")
        params.append(course_id)
    else:
        # 换学期后不再跟踪的课不会再同步，它们的记录也不会被清掉，默认别列出来
        where.append("(course_id IN (SELECT id FROM courses WHERE tracked=1)"
                     " OR NOT EXISTS (SELECT 1 FROM courses WHERE tracked=1))")
    if not include_videos:
        where.append("reason != 'video'")
    if where:
        q += " WHERE " + " AND ".join(where)
    names = _course_names(con)
    out = []
    # 录像放最后：默认就不下，一般不是大家要找的
    for r in con.execute(q + " ORDER BY course_id, reason='video', folder, name", params):
        c = names.get(r["course_id"], {})
        out.append({"id": r["id"], "course": c.get("code") or str(r["course_id"]), "course_id": r["course_id"],
                    "path": display_path(r), "name": r["name"], "activity": r["title"], "bytes": r["size"],
                    "size": _mb(r["size"]) if r["size"] else "", "kind": r["file_kind"] or "",
                    "reason": r["reason"], "why": REASONS.get(r["reason"], r["reason"]), "url": r["cm_url"]})
    return out


def pick(rows: list[dict], wanted: list[str]) -> list[dict]:
    """--get 后面可以写编号，也可以写文件名的一部分（不分大小写）；all 是全部。"""
    if any(w.lower() == "all" for w in wanted):
        return rows
    chosen: dict[int, dict] = {}
    for w in wanted:
        w = w.strip()
        if w.isdigit():
            hit = [r for r in rows if r["id"] == int(w)]
        else:
            hit = [r for r in rows if w.lower() in r["path"].lower()]
        if not hit:
            raise LookupError(f"没找到 {w}：先运行 monash skipped 看编号")
        for r in hit:
            chosen[r["id"]] = r
    return list(chosen.values())


SPACE_MARGIN = 500 * 1024 * 1024     # 补完还要给系统留点空间


def total_bytes(rows: list[dict]) -> int:
    return sum(r.get("bytes") or 0 for r in rows)


def check_space(picked: list[dict]) -> None:
    """大文件补下之前看看硬盘够不够，不够就别开始，免得下到一半把盘塞满。"""
    import shutil
    need = total_bytes(picked)
    if not need:
        return
    syncer.FILES_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(syncer.FILES_DIR).free
    if free < need + SPACE_MARGIN:
        raise ValueError(f"硬盘剩 {_mb(free)}，这些文件一共要 {_mb(need)}，先清点空间再补下"
                      "（monash move 可以把数据搬到别的盘）")


def fetch(client, con, picked: list[dict], log=print) -> dict:
    """补下挑出来的文件，不管大小上限和类型设置。按课分批，每门课只走一遍。"""
    names = _course_names(con)
    done, failed = [], []
    for cid in sorted({r["course_id"] for r in picked}):
        meta = names.get(cid) or {"id": cid, "fullname": str(cid)}
        course = {"id": cid, "fullname": meta.get("fullname") or meta.get("code") or str(cid),
                  "shortname": meta.get("code") or ""}
        rows = {r["source"]: r for r in con.execute(
            "SELECT * FROM skipped WHERE course_id=? AND rowid IN (%s)" % ",".join("?" * len(picked)),
            (cid, *[p["id"] for p in picked]))}
        if not rows:
            continue
        s = CourseSync(client, con, course, log=log)
        s.videos, s.max_bytes, s.skip_types = True, None, set()   # 用户点名要的，设置不拦
        s.only_sources = set(rows)
        zips_done: set[int] = set()
        for source, r in rows.items():
            cm = {"id": r["cmid"], "name": r["cm_name"] or r["title"] or r["name"], "url": r["cm_url"]}
            try:
                if r["member"]:
                    if r["cmid"] not in zips_done:
                        zips_done.add(r["cmid"])
                        s.fetch_folder(cm, r["folder"], r["section"] or "", force=True)
                else:
                    s.fetch_file(source, r["folder"], cm, r["section"] or "", resolved=True)
            except Exception as e:  # noqa: BLE001  一个失败不影响别的
                if e.__class__.__name__ == "MoodleAuthError":
                    raise
                failed.append({"path": display_path(r), "error": str(e)})
                continue
        root = course_folder(course)
        touched: set[str] = set()
        for source, r in rows.items():
            got = con.execute("SELECT path FROM files WHERE course_id=? AND source=?", (cid, source)).fetchone()
            if not got or not (syncer.FILES_DIR / got["path"]).exists():
                if not any(f["path"] == display_path(r) for f in failed):
                    failed.append({"path": display_path(r), "error": "没下载到（Moodle 上可能已经没有了）"})
                continue
            done.append({"path": got["path"], "bytes": (syncer.FILES_DIR / got["path"]).stat().st_size})
            con.execute("DELETE FROM skipped WHERE course_id=? AND source=?", (cid, source))
            con.execute("DELETE FROM notes WHERE course_id=? AND kind='skipped' AND url=?", (cid, source))
            for link in con.execute("SELECT folder FROM links WHERE course_id=? AND url=?", (cid, source)).fetchall():
                touched.add(link["folder"])
            con.execute("DELETE FROM links WHERE course_id=? AND url=?", (cid, source))
            if r["member"] and not con.execute("SELECT 1 FROM skipped WHERE course_id=? AND cmid=? AND member=1",
                                                (cid, r["cmid"])).fetchone():
                sub = f"{r['folder']}/{safe_name(r['cm_name'] or '')}"
                con.execute("DELETE FROM links WHERE course_id=? AND folder=? AND url=?", (cid, sub, r["cm_url"]))
                touched.add(sub)
        con.commit()
        write_links_md(con, cid, root)
        for folder in touched:      # 整个文件夹都没有链接了，write_links_md 不会碰它，旧的 links.md 要自己删
            if not con.execute("SELECT 1 FROM links WHERE course_id=? AND folder=?", (cid, folder)).fetchone():
                (syncer.FILES_DIR / root / folder / "links.md").unlink(missing_ok=True)
    if done:
        from . import content_index
        content_index.index(con, only_paths={d["path"] for d in done})
    return {"downloaded": done, "failed": failed}
