"""monash ed …：查本地的 Ed 库（终端里是文本，管道/重定向时是 JSON，可用 --json/--text 强制）。

    monash ed new                        # 上次看过之后的新帖/新回复
    monash ed threads FIT2102 --since 7d --unanswered
    monash ed search FIT2102 assignment deadline
    monash ed show FIT2102#42 [--live]   # 正文 + 全部回复；--live 先从 Ed 重抓这一帖
    monash ed following                  # 有未读新回复的帖子（我发的/关注的排前面）
    monash ed lessons FIT2109 --module "Week 3"
    monash ed quiz FIT2109               # Lessons 里的测验题（复习用；Ed 不公开答案）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from . import edlib, edquery
from .edquery import STATE_FILTERS


def _want_json(args) -> bool:
    if getattr(args, "json", False):
        return True
    if getattr(args, "text", False):
        return False
    return not sys.stdout.isatty()


def _emit(args, data, render) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2) if _want_json(args) else render(data))
    return 0


def _when(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _thread_line(t: dict) -> str:
    flag = ("📌" if t["is_pinned"] else "") + ("🔒" if t["is_private"] else "")
    if t["type"] == "question":
        flag += "✅" if t["is_staff_answered"] or t["is_answered"] else "❓"
    elif t["type"] == "announcement":
        flag += "📣"
    if t.get("is_starred"):
        flag += "★"
    unread = f"  (+{t['new_reply_count']} 未读)" if t.get("new_reply_count") else ""
    return f"{t['ref']:<14} {_when(t['created_at'])}  {flag:<3} 💬{t['reply_count']:<3} {t['title']}{unread}"


def _render_threads(rows: list[dict]) -> str:
    if not rows:
        return "（没有匹配的帖子）"
    out = []
    for t in rows:
        out.append(_thread_line(t))
        if t.get("snippet"):
            out.append(f"{'':<16}{t['snippet']}")
    return "\n".join(out)


def _render_replies(t: dict, width: int = 160) -> list[str]:
    out = []
    for r in t["new_replies"]:
        who = r["author"] + ("（教师/助教）" if r["is_staff"] else "")
        text = " ".join((r["text"] or "").split())
        out.append(f"{'':<16}↳ {who} {_when(r['created_at'])}：{text[:width]}{'…' if len(text) > width else ''}")
    return out


def cmd_courses(args):
    rows = edquery.courses(edlib.db_connect())
    return _emit(args, rows, lambda rs: "\n".join(
        f"{r['id']:>7}  {'★' if r['tracked'] else ' '} {r['code']:<16} {r['threads']:>4} 帖  "
        f"最新 {_when(r['latest'])}  {r['name']}" for r in rs) or "（还没同步过 Ed：monash sync）")


def cmd_threads(args):
    state = [k for k in STATE_FILTERS if getattr(args, k, False)]
    rows = edquery.list_threads(edlib.db_connect(), args.course, args.since, args.category, args.type,
                                args.unanswered, args.limit, args.offset, state)
    return _emit(args, rows, _render_threads)


def cmd_search(args):
    conn = edlib.db_connect()
    course, words = args.course, list(args.words)
    if course:
        try:                                   # 第一个词是课号就当课程，否则当搜索词
            edquery.resolve_course(conn, course)
        except edquery.NotFound:
            words.insert(0, course)
            course = None
    rows = edquery.search(conn, " ".join(words), course, args.since, args.category, args.type, args.limit)
    return _emit(args, rows, _render_threads)


def cmd_show(args):
    from . import edsync
    conn = edlib.db_connect()
    if args.live:
        try:
            tid = edquery.resolve_thread(conn, args.thread)
        except edquery.NotFound:
            if not str(args.thread).isdigit():
                raise
            tid = int(args.thread)
        try:
            edsync.refresh_thread(conn, tid)
        except edlib.EdForbidden:
            print("[注意] Ed 上已经看不到这个帖子了（被删除或改成私密），下面是库里的旧副本。", file=sys.stderr)
        args.thread = str(tid)
    return _emit(args, edquery.get_thread(conn, args.thread), edquery.thread_markdown)


def _render_new(d: dict) -> str:
    out = [f"自 {_when(d['since'])} 以来（最近一次同步 {_when(d['last_sync'])}）"]
    if d["new_threads"]:
        out += ["", f"## 新帖 {len(d['new_threads'])}", _render_threads(d["new_threads"])]
    if d["threads_with_new_replies"]:
        out += ["", f"## 有新回复 {len(d['threads_with_new_replies'])}"]
        for t in d["threads_with_new_replies"]:
            out.append(_thread_line(t))
            out += _render_replies(t, 140)
    if len(out) == 1:
        out.append("没有新动态。")
    return "\n".join(out)


def cmd_new(args):
    d = edquery.new_activity(edlib.db_connect(), args.since, args.course, cursor="cli",
                             advance=not args.peek, limit=args.limit)
    return _emit(args, d, _render_new)


def _render_following(rows: list[dict]) -> str:
    if not rows:
        return "（没有未读的新回复）"
    out = []
    for t in rows:
        who = "，".join(w for w, on in (("我发的", t["is_mine"]), ("关注", t["is_watched"]),
                                        ("收藏", t["is_starred"])) if on)
        out += ["", _thread_line(t) + (f"  [{who}]" if who else "")] + _render_replies(t)
    return "\n".join(out).lstrip("\n")


def cmd_following(args):
    return _emit(args, edquery.following(edlib.db_connect(), args.course, args.limit), _render_following)


def _render_lessons(rows: list[dict]) -> str:
    if not rows:
        return "（没有课程内容；先 monash sync）"
    out, last = [], None
    mark = {"completed": "✅", "attempted": "◐", "unattempted": "○"}
    for x in rows:
        if x["module"] != last:
            out += ([""] if out else []) + [f"## {x['module'] or '（未分组）'}"]
            last = x["module"]
        due = f"  截止 {_when(x['due_at'])}" if x["due_at"] else ""
        out.append(f"{mark.get(x['status'], '·')} {x['title']}  [{x['type']}]{due}")
        for f in x["files"]:
            where = f["file_url"] or f["url"] or "（正文已存到本地，monash grep 能搜到）"
            out.append(f"     {'📄' if f['type'] == 'pdf' else '🔗' if f['type'] == 'webpage' else '📝'} "
                       f"{f['title'] or f['type']}  {where}")
    return "\n".join(out)


def cmd_lessons(args):
    return _emit(args, edquery.lessons(edlib.db_connect(), args.course, args.module, args.status), _render_lessons)


def cmd_quiz(args):
    return _emit(args, edquery.quizzes(edlib.db_connect(), args.course, args.module, args.status),
                 edquery.quiz_markdown)


def add_commands(sub) -> None:
    def out(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        g = p.add_mutually_exclusive_group()
        g.add_argument("--json", action="store_true", help="输出 JSON（管道里默认就是）")
        g.add_argument("--text", action="store_true", help="输出文本（终端里默认就是）")
        p.set_defaults(fn=fn)
        return p

    def filters(p):
        p.add_argument("--since", help="只看这之后发的：7d / 12h / 2w / 2026-09-01")
        p.add_argument("--category", help="分类（或子分类）包含这个词")
        p.add_argument("--type", choices=["question", "post", "announcement"])
        p.add_argument("--limit", type=int, default=20)

    out("courses", cmd_courses, help="本地库里的课程")
    p = out("threads", cmd_threads, help="列帖子（最新在前）")
    p.add_argument("course", nargs="?", help="课号；不给就是全部跟踪的课")
    filters(p)
    p.add_argument("--unanswered", action="store_true", help="只看还没人回答的提问")
    p.add_argument("--starred", action="store_true", help="只看我收藏的")
    p.add_argument("--watching", action="store_true", help="只看我关注的")
    p.add_argument("--mine", action="store_true", help="只看我发的")
    p.add_argument("--unseen", action="store_true", help="只看还没点开过的")
    p.add_argument("--unread-replies", dest="unread_replies", action="store_true", help="只看有新回复的")
    p.add_argument("--offset", type=int, default=0)
    p = out("search", cmd_search, help="搜标题、正文和回复（每个词都要出现）")
    p.add_argument("course", help="课号（可省略：第一个词不是课号时就当搜索词）")
    p.add_argument("words", nargs="*")
    filters(p)
    p = out("show", cmd_show, help="读一个帖子：正文 + 全部回复")
    p.add_argument("thread", help="FIT2102#42、帖子 id 或 Ed 链接")
    p.add_argument("--live", action="store_true", help="先从 Ed 重抓这一帖")
    p = out("new", cmd_new, help="上次看过之后的新帖和新回复")
    p.add_argument("course", nargs="?")
    p.add_argument("--since", help="不用游标，看这之后的：7d / 2026-09-20")
    p.add_argument("--peek", action="store_true", help="看完不推进游标")
    p.add_argument("--limit", type=int, default=50)
    p = out("following", cmd_following, help="有未读新回复的帖子")
    p.add_argument("course", nargs="?")
    p.add_argument("--limit", type=int, default=30)
    for name, fn, h in (("lessons", cmd_lessons, "课程内容（Lessons）和课件"),
                        ("quiz", cmd_quiz, "Lessons 里的测验题（题面+选项）")):
        p = out(name, fn, help=h)
        p.add_argument("course")
        p.add_argument("--module", help="模块名的一部分，如 'Week 3'")
        p.add_argument("--status", help="completed / attempted / unattempted / all")
