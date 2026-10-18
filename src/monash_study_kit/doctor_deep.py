"""monash doctor --deep：拿第二个来源核对工具给出的结果，确认解析没坏、同步没漏。

普通的 doctor 只看配置和登录；这里真去 Moodle 和 Ed 拉一遍，再和另一处对照：
  截止日期      Moodle 接口  ↔  iCal 日历订阅
  作业          作业汇总页（解析 HTML）  ↔  课程结构接口里的作业
  课件          课程结构接口里的文件  ↔  本地已经同步的文件
  全文搜索      磁盘上的文件  ↔  索引，再随机搜几个词看能不能搜回原文件
  Ed 帖子       Ed 接口最新一页  ↔  本地库
  成绩          成绩总览页  ↔  跟踪的课

只读：不下载、不改库、不自动选课。输出只有数量、课号、日期和活动编号，
不打印标题、文件名、帖子内容、成绩，可以直接发给帮忙的人。
"""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import content_index as CI
from . import edlib, edsync, features as F, jobs
from .doctor import FAIL, OK, WARN, _check, scrub
from .moodlelib import FILES_DIR, MoodleAuthError, MoodleClient, db_connect
from .paths import load_settings
from .syncer import VIDEO_EXT, course_code

DAYS = 14
DUE_KINDS = {"due", "due date", "closes", "cut-off date"}
SAMPLE = 3          # 每类差异最多举几个例子
RESOLVE_MAX = 10    # 每门课最多为了分辨“录像没下”多问几次 Moodle
# 只挑完整的单词：网址里的一段（比如 Google Docs 链接里的 ID）在索引里是另一个词，搜不回来不算错
WORD_RE = re.compile(r"(?<!\w)[A-Za-z]{7,}(?!\w)")
URL_RE = re.compile(r"\S+://\S+|www\.\S+")


class Context:
    """几项检查共用一个 Moodle 会话和课程列表，免得每项都重新拉。"""

    def __init__(self, client: MoodleClient | None = None, con=None):
        self._client, self._courses = client, None
        self.con = con or db_connect()

    @property
    def client(self) -> MoodleClient:
        if self._client is None:
            self._client = MoodleClient()
        return self._client

    @property
    def tracked(self) -> list[dict]:
        if self._courses is None:
            ids = {r["id"] for r in self.con.execute("SELECT id FROM courses WHERE tracked=1")}
            self._courses = [c for c in self.client.courses() if c["id"] in ids]
        return self._courses


def _day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d") if ts else "?"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _examples(items) -> str:
    items = list(items)
    more = f" 等 {len(items)} 项" if len(items) > SAMPLE else ""
    return "、".join(items[:SAMPLE]) + more


# ---------------------------------------------------------------- 各项核对

def deep_due(ctx: Context) -> dict:
    name = "核对：截止日期"
    url = F.load_calendar_url()
    if not url:
        return _check(name, WARN, "没有日历订阅链接，没法对照", "monash moodle calendar --refresh")
    now = int(time.time())
    codes = {course_code(c) for c in ctx.tracked}
    api = {i["id"]: i for i in F.due(ctx.client, days=DAYS, include_overdue_days=0)
           if i["kind"] in DUE_KINDS and i["due_ts"] >= now}
    ical = {i["id"]: i for i in F.ical_items(F.fetch_ical(url), now)
            if i["kind"] in DUE_KINDS and now <= (i["due_ts"] or 0) <= now + DAYS * 86400
            and (not codes or i["course"] in codes)}
    only_ical = [f"{ical[k]['course']} {_day(ical[k]['due_ts'])}" for k in ical if k not in api]
    only_api = [f"{api[k]['course']} {_day(api[k]['due_ts'])}" for k in api if k not in ical]
    detail = f"未来 {DAYS} 天：接口 {len(api)} 条，日历订阅 {len(ical)} 条"
    if only_ical:
        return _check(name, WARN, f"{detail}；日历里有、工具里没有：{_examples(only_ical)}",
                      "把这段发给维护的人：截止日期的接口解析可能漏了")
    if only_api:
        detail += f"；接口多出 {len(only_api)} 条（日历订阅只导出部分事件，正常）"
    return _check(name, OK, detail + "，一致" if not only_api else detail)


def deep_assignments(ctx: Context) -> dict:
    name = "核对：作业"
    bad, total = [], 0
    for c in ctx.tracked:
        state = ctx.client.course_state(c["id"])
        want = {int(cm["id"]) for cm in state["cm"] if cm.get("module") == "assign" and cm.get("uservisible", True)}
        got = {a["cmid"] for a in F.assignments(ctx.client, c) if a["cmid"]}
        total += len(want)
        if want - got:
            bad.append(f"{course_code(c)} 少了 {len(want - got)} 个（cmid {_examples(str(i) for i in sorted(want - got))}）")
    detail = f"{len(ctx.tracked)} 门课，课程结构里 {total} 个作业"
    if bad:
        return _check(name, WARN, f"{detail}；作业汇总页没解析出来：{'；'.join(bad)}",
                      "把这段发给维护的人：作业页的格式可能变了")
    return _check(name, OK, detail + "，作业汇总页都解析到了")


def deep_files(ctx: Context) -> dict:
    name = "核对：课件"
    videos = bool(load_settings().get("download_videos"))
    missing, stale, gone_local, expected, total = [], [], [], 0, 0
    for c in ctx.tracked:
        state = ctx.client.course_state(c["id"])
        cms = {int(cm["id"]): cm for cm in state["cm"] if cm.get("uservisible", True)}
        rows = ctx.con.execute("SELECT cmid, path FROM files WHERE course_id=? AND removed_at IS NULL",
                               (c["id"],)).fetchall()
        have = {r["cmid"] for r in rows}
        # stealth：老师设成“能打开但课程页上不显示”，同步按课程页走，本来就不下
        want = [i for i, cm in cms.items() if cm.get("module") == "resource" and cm.get("url")
                and not cm.get("stealth")]
        total += len(want)
        lost = [i for i in want if i not in have]
        for i in lost[:RESOLVE_MAX]:
            # 录像默认不下载、只记链接：问一下 Moodle 文件名，是录像就不算漏
            # 文件名看本站最后那个地址（同步也是按它判断）；CloudFront 的地址常常没有扩展名
            local, external, _ = ctx.client.resolve(cms[i]["url"] + "&redirect=1")
            names = [u.split("?", 1)[0].lower() for u in (local, external) if u]
            if not videos and any(n.endswith(VIDEO_EXT) for n in names):
                expected += 1
            else:
                missing.append(f"{course_code(c)} cmid {i}")
        missing += [f"{course_code(c)} cmid {i}" for i in lost[RESOLVE_MAX:]]
        stale += sorted({f"{course_code(c)} cmid {r['cmid']}" for r in rows if r["cmid"] and r["cmid"] not in cms})
        gone_local += [r["path"] for r in rows if not (FILES_DIR / r["path"]).exists()]
    detail = f"{len(ctx.tracked)} 门课，Moodle 上 {total} 个文件资源" + (f"（{expected} 个是没下载的录像）" if expected else "")
    problems = []
    if missing:
        problems.append(f"Moodle 上有、本地没有：{_examples(missing)}")
    if stale:
        problems.append(f"Moodle 上已经没有、本地还当作在：{_examples(stale)}")
    if gone_local:
        problems.append(f"库里有记录、磁盘上找不到的文件 {len(gone_local)} 个")
    if problems:
        return _check(name, WARN, f"{detail}；" + "；".join(problems), "monash sync；同步完还是这样就发给维护的人")
    return _check(name, OK, detail + "，本地都有")


def deep_index(ctx: Context, probes: int = 5) -> dict:
    name = "核对：全文搜索"
    con = ctx.con
    CI.ensure_schema(con)
    known = {r["path"]: r for r in con.execute("SELECT path, mtime, size, chunks, course FROM content_files")}
    files = [f for f in CI.candidates() if Path(f["path"]).exists()
             and Path(f["path"]).stat().st_size <= CI.MAX_FILE]
    not_indexed = [f for f in files if f["path"] not in known
                   or (known[f["path"]]["mtime"], known[f["path"]]["size"])
                   != (Path(f["path"]).stat().st_mtime, Path(f["path"]).stat().st_size)]
    orphans = [p for p in known if not Path(p).exists()]
    chunk_counts = dict(con.execute("SELECT path, count(*) FROM content_fts GROUP BY path").fetchall())
    broken = [p for p, r in known.items() if (r["chunks"] or 0) > 0 and chunk_counts.get(p, 0) != r["chunks"]]

    # 搜索本身：从随机几个文件里各挑一个词去搜，原文件应该在结果里
    hits = tried = 0
    sample = [p for p in chunk_counts if Path(p).exists()]
    for path in random.Random(0).sample(sample, min(probes, len(sample))):
        row = con.execute("SELECT text FROM content_fts WHERE path = ? LIMIT 1", (path,)).fetchone()
        words = WORD_RE.findall(URL_RE.sub(" ", row[0] if row else ""))
        if not words:
            continue
        tried += 1
        word = max(words, key=len)
        found = [h for h in CI.search(con, word, course=known[path]["course"] if path in known else None, limit=500)
                 if h["abs_path"] == path]
        # 搜到了还不够：给 read_file 的路径也要能找回同一个文件
        if found and found[0]["path"] and CI.resolve_readable(found[0]["path"]) == Path(path).resolve():
            hits += 1
    detail = f"{len(files)} 个文件，已索引 {len(files) - len(not_indexed)} 个；抽查搜索 {hits}/{tried} 次搜回原文件"
    problems = []
    if not_indexed:
        problems.append(f"{len(not_indexed)} 个还没进索引（加密或损坏的文件也会这样）")
    if orphans:
        problems.append(f"索引里 {len(orphans)} 个文件已经不在磁盘上")
    if broken:
        problems.append(f"{len(broken)} 个文件的索引不完整")
    if tried and hits < tried:
        problems.append("有的词搜不回原文件")
    if problems:
        status = WARN if not (broken or (tried and hits < tried)) else FAIL
        return _check(name, status, f"{detail}；" + "；".join(problems), "monash sync 会重建索引；还不行就发给维护的人")
    return _check(name, OK, detail)


def deep_ed(ctx: Context, limit: int = 30) -> dict:
    name = "核对：Ed 帖子"
    if not jobs.have_ed_token():
        return _check(name, WARN, "没有 Ed 令牌，跳过", "monash login ed")
    client, conn = edlib.EdClient(), edlib.db_connect()
    try:
        courses = conn.execute("SELECT id, code FROM courses WHERE tracked=1").fetchall()
        run = conn.execute("SELECT started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        since = _parse_time(run["started_at"] if run else None)
        missing, stale, newer, total = [], [], 0, 0
        for c in courses:
            live = client.get(f"/courses/{c['id']}/threads", {"limit": limit, "offset": 0, "sort": "new"}).get("threads", [])
            local = {r["id"]: r["content_hash"] for r in
                     conn.execute("SELECT id, content_hash FROM threads WHERE course_id=?", (c["id"],))}
            code = edsync.short_code(c["code"])
            total += len(live)
            for t in live:
                if t["id"] in local and local[t["id"]] == edsync.content_stamp(t):
                    continue
                # 上次同步开始之后才发或才改的帖子，下次同步就有了，不算漏
                changed = _parse_time(t.get("updated_at") or t.get("created_at"))
                if since and changed and changed >= since:
                    newer += 1
                elif t["id"] in local:
                    stale.append(f"{code}#{t.get('number')}")
                else:
                    missing.append(f"{code}#{t.get('number')}")
    finally:
        conn.close()
    detail = f"{len(courses)} 门课最新的 {total} 个帖子" + (f"（{newer} 个是上次同步之后才有变化的）" if newer else "")
    if missing or stale:
        parts = ([f"本地没有：{_examples(missing)}"] if missing else []) + \
                ([f"本地是旧的：{_examples(stale)}"] if stale else [])
        hours = jobs.hours_since_sync()
        fix = "monash sync" + ("（上次同步是 1 小时内，还对不上就发给维护的人）" if hours < 1 else "")
        return _check(name, WARN, f"{detail}；" + "；".join(parts), fix)
    return _check(name, OK, detail + "，本地都是最新的")


def deep_grades(ctx: Context) -> dict:
    name = "核对：成绩"
    seen = {g["course_id"] for g in F.grades_overview(ctx.client)}
    lost = [course_code(c) or str(c["id"]) for c in ctx.tracked if c["id"] not in seen]
    detail = f"成绩总览里 {len(seen)} 门课"
    if lost:
        return _check(name, WARN, f"{detail}；跟踪的课不在里面：{_examples(lost)}",
                      "刚开学的课可能还没有成绩表；一直这样就发给维护的人")
    return _check(name, OK, detail + "，跟踪的课都在")


DEEP_CHECKS = [deep_due, deep_assignments, deep_files, deep_index, deep_ed, deep_grades]
NAMES = {"deep_due": "核对：截止日期", "deep_assignments": "核对：作业", "deep_files": "核对：课件",
         "deep_index": "核对：全文搜索", "deep_ed": "核对：Ed 帖子", "deep_grades": "核对：成绩"}


def run() -> list[dict]:
    """每项独立，一项出错不影响别的。Moodle 没登录就只做不需要 Moodle 的那几项。"""
    ctx = Context()
    out = []
    try:
        for fn in DEEP_CHECKS:
            try:
                out.append(fn(ctx))
            except MoodleAuthError:
                out.append(_check(NAMES[fn.__name__], FAIL, "Moodle 没登录，核对不了", "monash login"))
            except Exception as e:  # noqa: BLE001
                out.append(_check(NAMES[fn.__name__], WARN, f"核对时出错：{type(e).__name__}: {scrub(str(e))[:120]}"))
    finally:
        ctx.con.close()
    return out
