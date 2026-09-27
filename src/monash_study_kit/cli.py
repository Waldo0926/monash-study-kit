"""monash —— Monash 学习助手命令行。

    monash setup                 # 第一次用：登录 Ed 和 Moodle、选课、接进 Claude
    monash login                 # Moodle 登录过期了：打开专用登录窗口
    monash login ed              # 换 Ed 令牌
    monash status                # 登录状态、上次同步
    monash doctor                # 出问题时：一项项检查并告诉你怎么修
    monash help                  # 能做的事，每条附示例问法
    monash sync                  # 立刻同步 Ed + Moodle（Claude 开着时每小时自动一次）
    monash courses               # 选要跟踪的课

    monash todo                  # 本周待办：Moodle 截止/漏交 + Ed 没做完的 lesson、公告、未读回复
    monash due [FIT2102]         # 截止日期
    monash grep "monad" [FIT2102]  # 全文搜课件、字幕稿，带页码/时间戳
    monash moodle …              # 更多 Moodle 查询：grades、news、assignments、find、get、messages…
    monash ed …                  # Ed 查询：new、threads、search、show、following、lessons、quiz

    monash media                 # 录播：取 YouTube 字幕、下载 Zoom 录像并转写（要装 [media]）
    monash open                  # 打开课件文件夹
    monash config                # 看/改设置
    monash update                # 更新到最新版
    monash uninstall             # 从 Claude 里移除（可选删除数据）

大部分查询加 --json 输出 JSON（输出被管道接走时自动是 JSON，--text 强制文本）。
退出码：0 成功，1 参数不对，2 需要登录，3 连不上 Moodle/Ed 或对方出错，4 找不到课程/帖子，130 被中断。
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import urllib.error
import webbrowser

from . import __version__, edlib, jobs
from . import features as F
from .edlib import EdAuthError
from .moodlelib import FILES_DIR, MoodleAuthError, MoodleClient, MoodleError, db_connect
from .output import emit_json, local_time, relative, table, want_json
from .paths import HOME, ensure_private_dir, load_settings, save_settings
from .syncer import course_code, course_folder


def _fmt_secs(s: int | None) -> str:
    if s is None:
        return "未知"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分" if s >= 3600 else f"{s // 60} 分"


def _yes(prompt: str, default: bool = True) -> bool:
    ans = input(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    return default if not ans else ans in ("y", "yes", "是")


# ---------------------------------------------------------------- 登录

def login_moodle(interactive_only: bool = False) -> bool:
    from .browser_login import LoginFailed, ensure_login, login_moodle as window_login
    try:
        r = window_login(interactive=True) if interactive_only else ensure_login()
    except LoginFailed as e:
        print(f"Moodle 没登录成功：{e}")
        return False
    print(f"✓ Moodle 已登录（会话还剩 {_fmt_secs(r['time_remaining'])}；Claude 开着时会自动续期）")
    return True


def login_ed() -> bool:
    print("Ed 用“API 令牌”登录（不会过期，随时可以在 Ed 设置页删掉作废）：")
    print(f"  1. 在浏览器里打开 {edlib.TOKEN_PAGE}（正在帮你打开）")
    print("  2. 点 Create Token，名字随便写（比如 claude），复制生成的令牌")
    print("  3. 粘贴到下面（不会显示出来），回车")
    webbrowser.open(edlib.TOKEN_PAGE)
    for _ in range(3):
        token = getpass.getpass("Ed 令牌：").strip()
        if not token:
            print("没有输入，跳过 Ed。之后可以运行 monash login ed")
            return False
        if any(c.isspace() for c in token):
            print("令牌里不应该有空格或换行，再粘贴一次")
            continue
        try:
            user = edlib.EdClient(token=token).me().get("user", {})
        except Exception as e:  # noqa: BLE001 —— 格式不对 Ed 回 400，删掉的回 401
            print(f"Ed 不认这个令牌（{getattr(e, 'code', e)}），再试一次")
            continue
        edlib.save_token(token)
        print(f"✓ Ed 已登录：{user.get('name')} <{user.get('email')}>")
        return True
    return False


def cmd_login(args):
    if args.what == "ed":
        return 0 if login_ed() else 1
    return 0 if login_moodle(interactive_only=args.window) else 2


# ---------------------------------------------------------------- 选课

def _pick(items: list[str], chosen: set[int], title: str) -> set[int] | None:
    print(f"\n{title}")
    for i, label in enumerate(items, 1):
        print(f"  {'[x]' if i - 1 in chosen else '[ ]'} {i:>2}. {label}")
    ans = input("输入要跟踪的编号（空格分隔，如 1 3 4；直接回车保持不变）：").strip()
    if not ans:
        return None
    out = set()
    for tok in ans.replace(",", " ").split():
        if "-" in tok and tok.replace("-", "").isdigit():
            a, b = tok.split("-", 1)
            out.update(range(int(a) - 1, int(b)))
        elif tok.isdigit():
            out.add(int(tok) - 1)
    return {i for i in out if 0 <= i < len(items)}


def choose_courses(interactive: bool = True) -> None:
    """Moodle 默认跟踪本学期的课，Ed 默认跟踪 active 的课；interactive 时让人改。"""
    try:
        client, con = MoodleClient(), db_connect()
        courses = sorted(jobs.load_courses(client, con), key=lambda c: -(c.get("startdate") or 0))
        tracked = {c["id"] for c in jobs.tracked_courses(con, courses)}
        if interactive:
            picked = _pick([course_folder(c) for c in courses],
                           {i for i, c in enumerate(courses) if c["id"] in tracked}, "Moodle 课程（★ 同步课件）：")
            if picked is not None:
                con.execute("UPDATE courses SET tracked=0")
                con.executemany("UPDATE courses SET tracked=1 WHERE id=?", [(courses[i]["id"],) for i in picked])
                con.commit()
                tracked = {courses[i]["id"] for i in picked}
        print("Moodle 跟踪：" + ("、".join(course_code(c) or course_folder(c) for c in courses if c["id"] in tracked) or "无"))
    except MoodleAuthError:
        print("（Moodle 没登录，先跳过 Moodle 选课）")
    if not jobs.have_ed_token():
        return
    from . import edsync
    try:
        ed = edlib.EdClient()
        conn = edlib.db_connect()
        ecs = ed.courses()
        edsync.upsert_courses(conn, ecs)
        if not conn.execute("SELECT 1 FROM courses WHERE tracked=1").fetchone():
            edsync.auto_track(conn, ecs)
        rows = [dict(r) for r in conn.execute("SELECT id, code, name, status, tracked FROM courses "
                                              "ORDER BY status != 'active', id DESC")]
        if interactive:
            picked = _pick([f"{edsync.short_code(r['code'])}  {r['name']}  ({r['status']})" for r in rows],
                           {i for i, r in enumerate(rows) if r["tracked"]}, "Ed 课程（★ 同步帖子和 Lessons）：")
            if picked is not None:
                conn.execute("UPDATE courses SET tracked=0")
                conn.executemany("UPDATE courses SET tracked=1 WHERE id=?", [(rows[i]["id"],) for i in picked])
                conn.commit()
        names = [edsync.short_code(r["code"]) for r in conn.execute("SELECT code FROM courses WHERE tracked=1")]
        print("Ed 跟踪：" + ("、".join(names) or "无"))
    except EdAuthError as e:
        print(f"（Ed：{e}）")


def cmd_courses(args):
    choose_courses(interactive=True)


# ---------------------------------------------------------------- setup / status / sync

def cmd_setup(args):
    from . import claude_setup
    if args.what == "claude":
        desktop = claude_setup.install_desktop()
        code = claude_setup.install_code()
        return 0 if desktop or code else 1
    print(f"Monash 学习助手 {__version__}\n数据都放在：{HOME}\n")
    print("== 1/4 Ed ==")
    if jobs.have_ed_token():
        print("✓ 已经有 Ed 令牌（要换就运行 monash login ed）")
    else:
        login_ed()
    print("\n== 2/4 Moodle ==")
    from .browser_login import session_ok
    if session_ok():
        print("✓ Moodle 已经是登录状态")
    else:
        print("接下来会打开一个单独的浏览器窗口，在里面正常登录 Monash（Okta + MFA）。")
        print("这个窗口用自己的配置目录，和你平时用的浏览器互不影响。")
        input("准备好了按回车…")
        login_moodle()
    print("\n== 3/4 选课 ==")
    choose_courses(interactive=_yes("默认跟踪本学期的课。要自己挑吗？", default=False))
    print("\n== 4/4 接进 Claude ==")
    claude_setup.install_code()
    claude_setup.install_desktop()
    print()
    if _yes("现在先同步一次吗？（第一次要几分钟；不同步的话，Claude 开着时也会在后台自己同步）"):
        cmd_sync(argparse.Namespace(dry_run=False, courses=[]))
    print("\n完成！在 Claude 里问问看：“这周我有什么要交的？”")
    return 0


def cmd_status(args):
    st = jobs.read_status()
    out = {"home": str(HOME), "files": str(FILES_DIR), "last_sync": st.get("last_sync"),
           "errors": st.get("errors") or {}}
    try:
        out["moodle_session_left"] = MoodleClient().time_remaining()
    except MoodleAuthError as e:
        out["moodle_problem"] = str(e)
    out["ed_token"] = jobs.have_ed_token()
    out["calendar_fallback"] = bool(F.load_calendar_url())
    if want_json(args):
        return emit_json(out)
    print(f"数据目录：{out['home']}")
    if "moodle_session_left" in out:
        print(f"Moodle：已登录，会话还剩 {_fmt_secs(out['moodle_session_left'])}")
    else:
        print(f"Moodle：未登录（{out['moodle_problem']}）→ monash login"
              + ("；截止日期暂时用日历订阅链接" if out["calendar_fallback"] else ""))
    print(f"Ed：{'已设置令牌' if out['ed_token'] else '没有令牌 → monash login ed'}")
    print(f"上次同步：{local_time(out['last_sync'], '%Y-%m-%d %H:%M') if out['last_sync'] else '还没同步过'}")
    for k, v in out["errors"].items():
        print(f"  ! {k}：{v}")


def cmd_doctor(args):
    from . import doctor
    report = doctor.run()
    if want_json(args):
        return emit_json(report)
    print(doctor.render(report))


def cmd_help(args):
    from . import help_menu
    print(help_menu.render(cli=True))


def cmd_sync(args):
    if args.courses or args.dry_run:
        client, con = MoodleClient(), db_connect()
        courses = jobs.load_courses(client, con)
        only = [F.find_course(courses, r) for r in args.courses] if args.courses else None
        st = jobs.sync_moodle(only=only, dry_run=args.dry_run)
        print(f"新下载 {st['downloaded']} 个文件，{st['bytes'] / 1e6:.1f} MB")
        return
    st = jobs.sync_all()
    m, e, idx = st.get("moodle") or {}, st.get("ed") or {}, st.get("index") or {}
    print("\n同步完成：" + "，".join(x for x in [
        f"Ed 新帖 {e.get('new_threads', 0)}、新回复 {e.get('new_replies', 0)}" if "ed" not in st["errors"] else "",
        f"Moodle 新文件 {m.get('downloaded', 0)} 个（{m.get('bytes', 0) / 1e6:.1f} MB）" if "moodle" not in st["errors"] else "",
        f"索引了 {idx.get('indexed', 0)} 个文件" if idx else ""] if x))
    for k, v in (st.get("errors") or {}).items():
        print(f"  ! {k}：{v}")


def cmd_open(args):
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    path = str(FILES_DIR)
    if sys.platform == "darwin":
        subprocess.run(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.run(["xdg-open", path])
    print(path)


def cmd_config(args):
    if not args.key:
        for k, v in load_settings().items():
            print(f"{k} = {v}")
        print("\n改：monash config download_videos true")
        return
    cur = load_settings()
    if args.key not in cur:
        raise ValueError(f"没有这个设置：{args.key}（可用：{', '.join(cur)}）")
    if args.value is None:
        print(cur[args.key])
        return
    v = args.value.lower()
    val = v in ("true", "yes", "1", "on") if isinstance(cur[args.key], bool) else type(cur[args.key])(args.value)
    save_settings({args.key: val})
    print(f"{args.key} = {val}（Claude Desktop 要重启一下才会用上新设置）")


def cmd_uninstall(args):
    from . import claude_setup
    path = claude_setup.desktop_config_path()
    if path and path.exists():
        if claude_setup.desktop_running():
            print("先完全退出 Claude Desktop 再运行（开着改配置会被覆盖）")
            return 1
        if claude_setup.remove_from_desktop_config(path):
            print("已从 Claude Desktop 移除 monash")
    import shutil
    if shutil.which("claude"):
        subprocess.run(["claude", "mcp", "remove", "-s", "user", "monash"], capture_output=True)
        print("已从 Claude Code 移除 monash")
    if _yes(f"要删除所有数据吗（课件、登录状态、登录窗口的配置）？{HOME}", default=False):
        shutil.rmtree(HOME, ignore_errors=True)
        print("已删除")
    print("最后运行 uv tool uninstall monash-study-kit 卸载程序本身")


SOURCE = "https://github.com/Waldo0926/monash-study-kit/archive/refs/heads/main.zip"


def cmd_update(args):
    import importlib.util
    import shutil
    media = importlib.util.find_spec("faster_whisper") is not None
    spec = f"monash-study-kit[media] @ {SOURCE}" if media else SOURCE
    cmd = ["uv", "tool", "install", "--reinstall", "--refresh", spec]
    if os.name == "nt" or not shutil.which("uv"):
        # Windows 上正在运行的 monash.exe（包括 Claude 开着的那个）不能被覆盖，要先退出 Claude 再在新窗口里跑
        print("先完全退出 Claude Desktop（托盘图标 → Quit），然后在新的 PowerShell 窗口里运行：\n")
        print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        return 0
    r = subprocess.run(cmd)
    if r.returncode == 0:
        from . import update_check
        update_check.clear()
        print("\n已更新。重启 Claude Desktop 后生效。")
    return r.returncode


def cmd_mcp(args):
    from . import mcp_server
    mcp_server.main()


def cmd_media(args):
    from . import recordings, transcribe
    client, con = MoodleClient(), db_connect()
    jobs.load_courses(client, con)
    st = recordings.sync_recordings(client, con)
    print(f"Ed 录播：Zoom 录像 {st['zoom']} 个，YouTube 字幕 {st['youtube']} 份，已有 {st['skipped']}"
          + (f"，{len(st['errors'])} 个错误" if st["errors"] else ""))
    transcribe.run(con, limit=args.limit, match=args.match, dry_run=args.dry_run,
                   **({"model_name": args.model} if args.model else {}))
    from . import content_index
    content_index.index(con)


# ---------------------------------------------------------------- Moodle 查询

def _courses_for(client, con, refs: list[str] | None) -> list[dict]:
    courses = jobs.load_courses(client, con)
    if refs:
        return [F.find_course(courses, r) for r in refs]
    return jobs.tracked_courses(con, courses)


def cmd_todo(args):
    from . import todo
    try:
        client = MoodleClient()
        courses = _courses_for(client, db_connect(), None)
    except MoodleAuthError:
        client, courses = None, []
    data = todo.build(client, courses, args.days)
    if want_json(args):
        return emit_json(data)
    for n in data["notes"]:
        print(f"! {n}")
    print(f"== Moodle 截止（{data['days']} 天内）==")
    _print_due(data["due"]) if data["due"] else print("（没有）")
    if data["possibly_missing"]:
        print("\n== 可能漏交（已过截止、没交、没分）==")
        for a in data["possibly_missing"]:
            print(f"{local_time(a['due'])}  [{a['course']}] {a['name']}")
    if data["ed_lessons"]:
        todo_ls = [x for x in data["ed_lessons"] if x["status"] != "attempted"]
        half = [x for x in data["ed_lessons"] if x["status"] == "attempted"]
        print("\n== Ed 还没开始的 lesson（到目前进度的下一周）==")
        for x in todo_ls:
            print(f"○ [{x['course']}] {x['title']}")
        if half:
            print(f"另外 {len(half)} 个做了一半：monash ed lessons 课号 --status attempted")
    if data["announcements"]:
        print(f"\n== 最近 {data['days']} 天的公告 ==")
        for n in data["announcements"]:
            print(f"{local_time(n['time'])}  [{n['course']}] {n['title']}  ({n['ref'] or 'Moodle'})")
    if data["ed_unread_replies"]:
        print("\n== Ed 上有未读新回复 ==")
        for t in data["ed_unread_replies"]:
            print(f"{t['ref']:<13} +{t['new']}  {t['title']}{'  ★我发的/关注的' if t['mine_or_watched'] else ''}")


KIND_ZH = {"due": "截止", "due date": "截止", "closes": "关闭", "is due": "截止", "opens": "开放",
           "cut-off date": "最终截止", "grading due": "批改截止", "expected": "预计完成", "event": "事件",
           "course": "课程事件", "user": "个人", "site": "全站"}


def _print_due(items):
    rows = [{**i, "when": local_time(i["due"]), "left": relative(i["due"]),
             "what": i["activity"] or i["name"], "kind": KIND_ZH.get(i["kind"], i["kind"])} for i in items]
    if items and all(i.get("source") == "ical" for i in items):
        print("（Moodle 没登录，以下来自日历订阅链接，没有提交状态；monash login 后恢复）")
    table(rows, [("when", "时间", 16), ("left", "剩余", 8), ("course", "课程", 8), ("kind", "类型", 6),
                 ("what", "内容", 60)])


def _client_or_none():
    try:
        return MoodleClient()
    except MoodleAuthError:
        return None


def cmd_due(args):
    client = _client_or_none()
    course = None
    if args.course:
        # 没登录时只能按课号过滤日历订阅链接里的事件
        course = F.find_course(jobs.load_courses(client, db_connect()), args.course) if client \
            else {"id": None, "shortname": args.course.upper(), "fullname": args.course.upper()}
    items = F.due(client, args.days, course)
    return emit_json(items) if want_json(args) else _print_due(items)


def cmd_grep(args):
    from . import content_index as CI
    hits = CI.search(db_connect(), args.query, args.course.upper() if args.course else None, args.limit)
    if want_json(args):
        return emit_json(hits)
    if not hits:
        print("没搜到。还没同步过的话先 monash sync。")
    for h in hits:
        where = f"{h['loc']}" + (f"（视频 {h['video']}）" if h["video"] else "")
        print(f"[{h['course'] or '?'}] {h['title']}  @ {where}\n    {h['snippet']}\n")


def cmd_m_overview(args):
    client, con = MoodleClient(), db_connect()
    left = client.time_remaining()
    due = F.due(client, 14)
    alerts = F.alerts(client, 5)
    courses = _courses_for(client, con, None)
    news, missing = [], []
    for c in courses:
        news += F.news(client, c, 2)
        try:
            missing += [a for a in F.assignments(client, c) if a["missing"]]
        except Exception as e:  # noqa: BLE001
            print(f"! {course_code(c)} 作业汇总没拿到：{e}", file=sys.stderr)
    news = sorted(news, key=lambda x: -(x.get("time_ts") or 0))[:5]
    if want_json(args):
        return emit_json({"session_time_remaining": left, "due": due, "alerts": alerts, "news": news,
                          "possibly_missing": missing})
    print(f"会话还剩 {_fmt_secs(left)}\n\n== 近 14 天截止 ==")
    _print_due(due)
    print(f"\n== 通知 == 未读通知 {alerts['unread_notifications']}，未读消息 {alerts['unread_messages']}")
    if missing:
        print("\n== 可能漏交（已过截止、没交、没分）==")
        for a in missing:
            print(f"{local_time(a['due'])}  [{a['course']}] {a['name']}")
    print("\n== 最新公告 ==")
    for n in news:
        print(f"{local_time(n['time'])}  [{n['course']}] {n['title']}")


def cmd_m_calendar(args):
    url = F.calendar_url(MoodleClient(), refresh=args.refresh) if args.refresh or not F.load_calendar_url() \
        else F.load_calendar_url()
    shown = url if args.show else F.mask_calendar_url(url)
    if want_json(args):
        return emit_json({"url": shown})
    print(shown)
    if not args.show:
        print("（--show 显示完整链接。它等于一把只读日历钥匙，别发到公开的地方）")
    print("订阅：苹果日历 → 文件 → 新建日历订阅；Google 日历 → 其他日历 → 通过网址添加。")


def cmd_m_assignments(args):
    client, con = MoodleClient(), db_connect()
    items = []
    for c in _courses_for(client, con, [args.course] if args.course else None):
        items += F.assignments(client, c)
    if args.missing:
        items = [i for i in items if i["missing"]]
    if want_json(args):
        return emit_json(items)
    rows = [{**i, "when": local_time(i["due"]), "flag": "⚠ 可能漏交" if i["missing"] else ""} for i in items]
    table(rows, [("course", "课程", 8), ("when", "截止", 16), ("status", "提交", 22), ("grade", "成绩", 8),
                 ("flag", "", 10), ("name", "作业", 60)])
    if any(i["missing"] for i in items):
        print("\n⚠ = 已过截止、没交、也没分。面试、现场展示之类本来就不用在线交，自己核对一下。")


def cmd_m_grades(args):
    client, con = MoodleClient(), db_connect()
    if not args.course:
        data = F.grades_overview(client)
        return emit_json(data) if want_json(args) else table(data, [("grade", "总分", 10), ("course", "课程", 70)])
    course = F.find_course(jobs.load_courses(client, con), args.course)
    data = F.grades(client, course["id"], args.graded_only)
    if want_json(args):
        return emit_json(data)
    table(data, [("item", "项目", 40), ("grade", "得分", 10), ("range", "满分", 10), ("percentage", "百分比", 8),
                 ("feedback", "反馈", 60)])


def cmd_m_news(args):
    client, con = MoodleClient(), db_connect()
    out = []
    for c in _courses_for(client, con, [args.course] if args.course else None):
        out += F.news(client, c, args.limit)
    out.sort(key=lambda x: -(x.get("time_ts") or 0))
    if want_json(args):
        return emit_json(out)
    for n in out:
        print(f"\n■ [{n['course']}] {n['title']}  —  {n['author']} · {local_time(n['time'])}")
        text = n["text"] if args.full else n["text"][:400] + ("…" if len(n["text"]) > 400 else "")
        print("  " + text.replace("\n", "\n  "))
        print(f"  {n['url']}")


def cmd_m_search(args):
    client, con = MoodleClient(), db_connect()
    cid = F.find_course(jobs.load_courses(client, con), args.course)["id"] if args.course else 1
    data = F.forum_search(client, args.query, cid, args.limit)
    if want_json(args):
        return emit_json(data)
    for p in data:
        print(f"\n■ {p['subject']}  —  {p['author']} · {local_time(p['time'])}")
        print("  " + (p["text"][:300] + ("…" if len(p["text"]) > 300 else "")).replace("\n", "\n  "))
        print(f"  {p['url']}")
    if not data:
        print("没有搜到。Monash 大部分课的讨论在 Ed 上（monash ed search …）。")


def cmd_m_find(args):
    client, con = MoodleClient(), db_connect()
    data = F.find(client, args.query, _courses_for(client, con, [args.course] if args.course else None),
                  limit=args.limit)
    if want_json(args):
        return emit_json(data)
    table(data, [("course", "课程", 8), ("type", "类型", 9), ("id", "id", 8), ("name", "名称", 45),
                 ("where", "位置", 60)])
    print("\n下载：monash moodle get <id>    打开：monash moodle open <id>")


def cmd_m_get(args):
    from pathlib import Path
    client, con = MoodleClient(), db_connect()
    receipts = F.get(client, args.ref, Path(args.to).expanduser(), args.force, jobs.load_courses(client, con))
    if want_json(args):
        return emit_json(receipts)
    for r in receipts:
        print(f"↓ {r['path']}  ({(r['bytes'] or 0) / 1e6:.2f} MB, {r['content_type']})" if r.get("path")
              else f"↗ {r['url']}（{r['note']}）")


def cmd_m_open(args):
    from .moodlelib import BASE_URL
    ref = args.ref
    if ref.startswith("http"):
        url = ref
    elif ref.isdigit() and len(ref) > 5 and not args.week:
        url = f"{BASE_URL}/mod/resource/view.php?id={ref}"
    else:
        client, con = MoodleClient(), db_connect()
        course = F.find_course(jobs.load_courses(client, con), ref)
        url = f"{BASE_URL}/course/view.php?id={course['id']}"
        if args.week:
            hits = F.find(client, f"week {args.week}", [course], types={"section"})
            url = hits[0]["url"] if hits else url
    print(url)
    webbrowser.open(url)


def cmd_m_alerts(args):
    data = F.alerts(MoodleClient(), args.limit)
    if want_json(args):
        return emit_json(data)
    print(f"未读通知 {data['unread_notifications']}，未读消息 {data['unread_messages']}")
    for n in data["notifications"]:
        print(f"{'  ' if n['read'] else '● '}{local_time(n['time'])}  {n['subject']}")


def cmd_m_messages(args):
    client = MoodleClient()
    if args.conversation:
        data = F.conversation_messages(client, args.conversation, args.limit)
        if want_json(args):
            return emit_json(data)
        for m in data["messages"]:
            print(f"{local_time(m['time'])}  {m['from']}：{m['text']}")
        return
    data = F.conversations(client, args.limit)
    if want_json(args):
        return emit_json(data)
    rows = [{**c, "flag": f"● {c['unread']}" if c["unread"] else "",
             "when": local_time((c["last"] or {}).get("time")),
             "preview": (f"{c['last']['from']}：{c['last']['text']}" if c["last"] else "").replace("\n", " ")}
            for c in data]
    table(rows, [("id", "id", 8), ("flag", "未读", 5), ("name", "对话", 24), ("when", "最后一条", 16),
                 ("preview", "内容", 60)])


def cmd_m_ls(args):
    con = db_connect()
    course_id = next((r["id"] for r in con.execute("SELECT id, code FROM courses ORDER BY id DESC")
                      if (r["code"] or "") == args.course.upper() or str(r["id"]) == args.course), None)
    if course_id is None:
        raise LookupError(f"本地没有课程 {args.course}（先 monash sync）")
    q, params = "SELECT path, size FROM files WHERE course_id=?", [course_id]
    if args.week:
        q += " AND path LIKE ?"
        params.append(f"%/Week {int(args.week):02d}%")
    rows = con.execute(q + " ORDER BY path", params).fetchall()
    for r in rows:
        print(f"{(r['size'] or 0) / 1e6:7.1f} MB  {r['path'].split('/', 1)[1]}")
    print(f"\n{len(rows)} 个文件，在 {FILES_DIR}")


# ---------------------------------------------------------------- 参数

# 退出码。argparse 默认参数错误退 2，会和"需要登录"撞车，所以 _Parser 改成退 1。
EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_NETWORK, EXIT_NOT_FOUND, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
EXIT_CODES = {
    EXIT_OK: "成功",
    EXIT_USAGE: "参数不对（看 stderr 的提示）",
    EXIT_AUTH: "需要登录：monash login（Moodle）或 monash login ed",
    EXIT_NETWORK: "连不上 Moodle/Ed，或对方返回了错误（稍后再试）",
    EXIT_NOT_FOUND: "找不到课程或帖子",
    EXIT_INTERRUPTED: "被 Ctrl-C 中断",
}


class _Parser(argparse.ArgumentParser):
    """参数错误退 EXIT_USAGE（argparse 默认退 2）。子命令的解析器也会用这个类。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    from . import cli_ed
    ap = _Parser(prog="monash", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"monash-study-kit {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    def cmd(parent, name, fn, json_flag=False, **kw):
        p = parent.add_parser(name, **kw)
        if json_flag:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--json", action="store_true", help="输出 JSON（管道里默认就是）")
            g.add_argument("--text", action="store_true", help="输出文本（终端里默认就是）")
        p.set_defaults(fn=fn)
        return p

    p = cmd(sub, "setup", cmd_setup, help="第一次用：登录、选课、接进 Claude")
    p.add_argument("what", nargs="?", choices=["claude"], help="claude：只做“接进 Claude”这一步")
    p = cmd(sub, "login", cmd_login, help="登录 Moodle（默认）或设置 Ed 令牌")
    p.add_argument("what", nargs="?", choices=["moodle", "ed"], default="moodle")
    p.add_argument("--window", action="store_true", help="直接开窗口，不先试后台自动登录")
    cmd(sub, "status", cmd_status, json_flag=True, help="登录状态、上次同步")
    cmd(sub, "help", cmd_help, help="能做的事和示例问法（Claude 里怎么问、对应哪条命令）")
    cmd(sub, "doctor", cmd_doctor, json_flag=True, help="体检：一项项查哪里不对，告诉你怎么修")
    p = cmd(sub, "sync", cmd_sync, help="同步 Ed + Moodle + 全文索引")
    p.add_argument("courses", nargs="*", help="只同步这几门 Moodle 课")
    p.add_argument("--dry-run", action="store_true", help="只列出 Moodle 会下载什么")
    cmd(sub, "courses", cmd_courses, help="选要跟踪的课")
    p = cmd(sub, "todo", cmd_todo, json_flag=True, help="本周待办")
    p.add_argument("--days", type=int, default=7)
    p = cmd(sub, "due", cmd_due, json_flag=True, help="截止日期")
    p.add_argument("course", nargs="?"); p.add_argument("--days", type=int, default=14)
    p = cmd(sub, "grep", cmd_grep, json_flag=True, help="全文搜课件和字幕稿")
    p.add_argument("query"); p.add_argument("course", nargs="?"); p.add_argument("--limit", type=int, default=15)
    p = cmd(sub, "media", cmd_media, help="录播：YouTube 字幕、Zoom 录像下载和转写（要装 [media]）")
    p.add_argument("--limit", type=int, default=0, help="这次最多转写几个视频")
    p.add_argument("--match", help="只转写路径里含这段文字的视频")
    p.add_argument("--model", help="whisper 模型（默认 small.en；慢的电脑可以用 base.en）")
    p.add_argument("--dry-run", action="store_true")
    cmd(sub, "open", cmd_open, help="打开课件文件夹")
    p = cmd(sub, "config", cmd_config, help="看/改设置")
    p.add_argument("key", nargs="?"); p.add_argument("value", nargs="?")
    cmd(sub, "uninstall", cmd_uninstall, help="从 Claude 移除，可选删除数据")
    cmd(sub, "update", cmd_update, help="更新到最新版")
    cmd(sub, "mcp", cmd_mcp, help="MCP 服务器（Claude 启动它，不用手动跑）")

    m = sub.add_parser("moodle", help="Moodle 查询").add_subparsers(dest="mcmd", required=True)
    cmd(m, "overview", cmd_m_overview, json_flag=True, help="近期截止、通知、公告、可能漏交")
    p = cmd(m, "due", cmd_due, json_flag=True); p.add_argument("course", nargs="?")
    p.add_argument("--days", type=int, default=14)
    p = cmd(m, "calendar", cmd_m_calendar, json_flag=True, help="iCal 订阅链接")
    p.add_argument("--refresh", action="store_true"); p.add_argument("--show", action="store_true")
    p = cmd(m, "assignments", cmd_m_assignments, json_flag=True, help="作业汇总")
    p.add_argument("course", nargs="?"); p.add_argument("--missing", action="store_true", help="只看可能漏交的")
    p = cmd(m, "grades", cmd_m_grades, json_flag=True); p.add_argument("course", nargs="?")
    p.add_argument("--graded-only", action="store_true")
    p = cmd(m, "news", cmd_m_news, json_flag=True); p.add_argument("course", nargs="?")
    p.add_argument("--limit", type=int, default=5); p.add_argument("--full", action="store_true")
    p = cmd(m, "search", cmd_m_search, json_flag=True, help="搜 Moodle 论坛"); p.add_argument("query")
    p.add_argument("course", nargs="?"); p.add_argument("--limit", type=int, default=30)
    p = cmd(m, "find", cmd_m_find, json_flag=True, help="找章节和活动"); p.add_argument("query")
    p.add_argument("course", nargs="?"); p.add_argument("--limit", type=int, default=20)
    p = cmd(m, "get", cmd_m_get, json_flag=True, help="下载单个资源"); p.add_argument("ref")
    p.add_argument("--to", default="."); p.add_argument("--force", action="store_true")
    p = cmd(m, "open", cmd_m_open, help="在浏览器里打开课程（或某周）"); p.add_argument("ref")
    p.add_argument("week", nargs="?")
    p = cmd(m, "alerts", cmd_m_alerts, json_flag=True); p.add_argument("--limit", type=int, default=10)
    p = cmd(m, "messages", cmd_m_messages, json_flag=True, help="站内私信（只读）")
    p.add_argument("conversation", nargs="?", type=int); p.add_argument("--limit", type=int, default=20)
    p = cmd(m, "ls", cmd_m_ls, help="本地已同步的课件"); p.add_argument("course"); p.add_argument("week", nargs="?")

    cli_ed.add_commands(sub.add_parser("ed", help="Ed 查询").add_subparsers(dest="ecmd", required=True))
    return ap


def _print_update_notice() -> None:
    """有新版本就在 stderr 提一行（只在终端里，别混进脚本和 agent 读的输出）。"""
    from . import update_check
    msg = update_check.notice()
    if msg and sys.stderr.isatty():
        print(f"\n↑ {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")    # 老终端的代码页打不出的字符别让程序崩掉
            else:
                stream.reconfigure(encoding="utf-8")    # 管道/重定向：Windows 默认是本地代码页，中文会丢
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    ensure_private_dir(HOME)      # 库里有同学的名字和私密帖，只给自己读
    quiet = args.cmd in ("mcp", "update", "doctor")   # mcp 的 stdout/stderr 归 Claude；另两个自己会说版本
    if not quiet:
        from . import update_check
        update_check.refresh_in_background()
    if not getattr(args, "fn", None):
        ap.print_help()
        print("\n不知道能做什么？运行 monash help 看功能大全和示例问法。")
        return EXIT_OK
    try:
        code = args.fn(args) or EXIT_OK
        if not quiet:
            _print_update_notice()
        return code
    except (MoodleAuthError, EdAuthError) as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_AUTH
    except LookupError as e:                   # 包括 edquery.NotFound
        print(e, file=sys.stderr)
        return EXIT_NOT_FOUND
    except ValueError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    except (MoodleError, urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # HTTPError 也是 URLError：Ed 的 401 已经变成 EdAuthError，走到这里的是 5xx 重试完之类
        print(f"连不上 Moodle/Ed，或对方出错：{e}", file=sys.stderr)
        return EXIT_NETWORK
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_OK
