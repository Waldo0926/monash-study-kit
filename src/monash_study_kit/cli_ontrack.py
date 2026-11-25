"""OnTrack / Doubtfire 命令行子命令：登录、课程/任务查询、读任务书、下载、同步与受控写操作。"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Any

from . import ontrack
from .output import emit_json, table, want_json


def _confirm_action(args, summary: str, details: dict[str, Any] | None = None) -> tuple[bool, int]:
    """写操作统一确认：--dry-run 只给预览；--confirm/--yes 直接放行；终端里交互确认。"""
    if getattr(args, "dry_run", False):
        if want_json(args):
            emit_json({"dry_run": True, "summary": summary, **(details or {})})
        else:
            print(f"[预览 / dry-run] {summary}")
        return False, 0
    if getattr(args, "confirm", False) or getattr(args, "yes", False):
        return True, 0
    if not sys.stdin.isatty():
        print(f"{summary}\n非交互环境下不会自动执行，请用户确认后加 --confirm 重试。", file=sys.stderr)
        return False, 1
    print(summary, file=sys.stderr)
    ans = input("确认执行？[y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("已取消。", file=sys.stderr)
        return False, 130
    return True, 0


def _read_pasted_secret() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("粘贴 Cookie / cURL / access-token JSON 或 refresh_token（不会回显）：").strip()
    return sys.stdin.read().strip()


def run(args) -> int:
    from .cli import EXIT_AUTH, EXIT_NETWORK, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE

    cmd = args.ocmd
    try:
        if cmd == "login":
            return _cmd_login(args)
        if cmd == "logout":
            res = ontrack.clear_session()
            if want_json(args):
                emit_json(res)
            else:
                print("已清除本机 OnTrack 登录缓存。" if res["removed_session"] else "本机没有已保存的 OnTrack 登录缓存。")
            return EXIT_OK
        if cmd == "status":
            return _cmd_status(args)
        if cmd == "renew":
            sess = ontrack.resolve_session(force_refresh=True)
            out = {
                "base_url": sess["base_url"],
                "username": sess["username"],
                "auth_token_expiry": sess.get("auth_token_expiry"),
                "provenance": sess.get("provenance"),
                "has_refresh_cookie": bool(sess.get("refresh_cookies")),
            }
            if want_json(args):
                emit_json(out)
            else:
                print(f"已续期：{out['username']} @ {out['base_url']}（访问令牌有效至 {out['auth_token_expiry']}）")
            return EXIT_OK
        if cmd == "doctor":
            return _cmd_doctor(args)
        if cmd == "user":
            data = ontrack.user_info(ontrack.OnTrackClient())
            if want_json(args):
                emit_json(data)
            else:
                name = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
                print(f"{data.get('username')}" + (f" ({name})" if name else "") + f" @ {data.get('base_url')}")
            return EXIT_OK
        if cmd == "sync":
            res = ontrack.sync(download_sheets=args.sheets)
            if want_json(args):
                emit_json(res)
            else:
                extra = f"，下载任务书 {res['sheets_downloaded']} 份" if args.sheets else ""
                print(f"OnTrack 同步完成：{res['units']} 门课、{res['tasks']} 个任务{extra}。")
                for err in res.get("errors") or []:
                    print(f"  ! {err}", file=sys.stderr)
            return EXIT_OK
        if cmd in ("courses", "units", "projects"):
            data = ontrack.OnTrackClient().projects(include_inactive=args.include_inactive)
            if want_json(args):
                emit_json(data)
            else:
                rows = [{
                    "id": p["id"],
                    "code": p["unit"]["code"],
                    "name": p["unit"]["name"],
                    "target": ontrack.grade_label(p.get("target_grade"), []),
                } for p in data]
                table(rows, [("id", "项目ID", 8), ("code", "课程", 12), ("name", "名称", 45), ("target", "目标等级", 16)])
            return EXIT_OK
        if cmd == "show":
            snap = ontrack.project_snapshot(ontrack.OnTrackClient(), args.unit)
            if want_json(args):
                emit_json(snap)
            else:
                u = snap["unit"]
                p = snap["project"]
                tg = ontrack.grade_label(p.get("target_grade"), u.get("grade_definitions", []))
                print(f"[{u['code']}] {u['name']}（项目 ID {p['id']}，目标 {tg}）")
                _print_tasks(snap["tasks"])
            return EXIT_OK
        if cmd == "tasks":
            rows = ontrack.list_tasks(ontrack.OnTrackClient(), args.unit, args.status)
            if want_json(args):
                emit_json(rows)
            else:
                _print_tasks(rows)
            return EXIT_OK
        if cmd == "task":
            detail = ontrack.task_detail(ontrack.OnTrackClient(), args.unit, args.task)
            if want_json(args):
                emit_json(detail)
            else:
                print(f"[{detail['unit_code']}] {detail['abbreviation']} {detail['name']}")
                print(f"状态：{detail['status_label']} ({detail['status']})")
                if detail.get("deadline"):
                    print(f"截止：{detail['deadline']}" + ("  [已逾期]" if detail.get("is_overdue") else ""))
                if detail.get("grade") is not None:
                    print(f"等级：{detail['grade_label']}")
                if detail.get("description"):
                    print(f"\n{detail['description']}")
                reqs = detail.get("upload_requirements") or []
                if reqs:
                    print("\n提交要求：")
                    for idx, r in enumerate(reqs, 1):
                        print(f"  {idx}. {r['name']} ({r['type']}, key={r['key']})")
            return EXIT_OK
        if cmd == "read":
            res = ontrack.read_task_sheet_markdown(
                ontrack.OnTrackClient(), args.unit, args.task, cache_pdf=not args.no_cache
            )
            if want_json(args):
                emit_json(res)
            else:
                print(res["markdown"], end="")
            return EXIT_OK
        if cmd == "get":
            return _cmd_get(args)
        if cmd == "set":
            return _cmd_set(args)
        if cmd == "submit":
            return _cmd_submit(args)
        if cmd == "chats":
            return _cmd_chats(args)
        if cmd == "chat-send":
            return _cmd_chat_send(args)
        if cmd == "roles":
            rows = ontrack.OnTrackClient().roles(active_only=not args.all)
            if want_json(args):
                emit_json(rows)
            else:
                flat = [{"id": r["id"], "role": r["role"], "code": r["unit"]["code"], "name": r["unit"]["name"]} for r in rows]
                table(flat, [("id", "ID", 8), ("role", "角色", 16), ("code", "课程", 12), ("name", "名称", 45)])
            return EXIT_OK
        raise ValueError(f"未知 ontrack 子命令：{cmd}")
    except ontrack.OnTrackAuthError as e:
        print(str(e), file=sys.stderr)
        return EXIT_AUTH
    except ontrack.OnTrackNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_NOT_FOUND
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    except ontrack.OnTrackError as e:
        print(f"OnTrack 出错：{e}", file=sys.stderr)
        return EXIT_NETWORK


def _print_tasks(rows: list[dict[str, Any]]) -> None:
    display = []
    for r in rows:
        due = r.get("deadline") or r.get("due_date") or "-"
        if r.get("is_overdue"):
            due += " !"
        unread = f"+{r['unread_comments']}" if r.get("unread_comments") else ""
        display.append({
            "abbr": r["abbreviation"],
            "name": r["name"],
            "status": r["status_label"],
            "due": due,
            "grade": r.get("grade_label") or "-",
            "chat": unread,
        })
    table(display, [
        ("abbr", "任务", 8),
        ("name", "名称", 34),
        ("status", "状态", 18),
        ("due", "截止", 13),
        ("grade", "等级", 14),
        ("chat", "未读", 5),
    ])


def _cmd_login(args) -> int:
    url = ontrack.configured_base_url(args.url)
    if args.paste:
        raw = _read_pasted_secret()
        sess = ontrack.login_paste(url, raw, username=args.username)
    else:
        if args.username:
            raise ValueError("--username 只能与 --paste 一起使用")
        sess = None
        if not args.window and (ontrack.paths.BROWSER_PROFILE / "Default").exists():
            try:
                sess = ontrack.login_browser(url, interactive=False, timeout=25.0, log=lambda *_: None)
            except ontrack.OnTrackAuthError:
                sess = None
        if sess is None:
            sess = ontrack.login_browser(
                url,
                interactive=True,
                timeout=float(args.timeout),
                log=lambda m: print(m, file=sys.stderr),
            )
    ontrack.enable_after_login(url, allow_write=bool(args.allow_write))
    out = {
        "logged_in": True,
        "base_url": sess["base_url"],
        "username": sess["username"],
        "auth_token_expiry": sess.get("auth_token_expiry"),
        "has_refresh_cookie": bool(sess.get("refresh_cookies")),
        "write_enabled": bool(args.allow_write),
    }
    if want_json(args):
        emit_json(out)
    else:
        print(f"✓ 已登录 OnTrack：{out['username']} @ {out['base_url']}")
        if not out["has_refresh_cookie"]:
            print("提示：未收到刷新 Cookie（登录时请勾选 Remember me），访问令牌到期后需要重新登录。", file=sys.stderr)
        print("现在可以运行 monash ontrack sync 同步到本地缓存，或在重启 AI 软件后直接使用 OnTrack 工具。")
    return 0


def _cmd_status(args) -> int:
    if args.local:
        data = ontrack.local_status()
    else:
        cl = ontrack.OnTrackClient()
        data = {**ontrack.local_status(), "live": ontrack.auth_check(cl)}
    if want_json(args):
        emit_json(data)
    else:
        print(f"OnTrack 站点：{data.get('base_url') or '未配置'}")
        print(f"状态：{'已登录' if data.get('configured') else '未登录'}"
              + (f"（{data['username']}）" if data.get("username") else ""))
        if data.get("access_token_expiry"):
            valid = "有效" if data.get("access_token_valid") else "需续期"
            print(f"访问令牌：{valid}（{data['access_token_expiry']}）")
        print(f"刷新 Cookie：{'有' if data.get('has_refresh_cookie') else '无'}"
              + (f"（到期 {data['refresh_cookie_expiry']}）" if data.get("refresh_cookie_expiry") else ""))
        print(f"本地缓存：{data.get('cached_units', 0)} 门课、{data.get('cached_tasks', 0)} 个任务"
              + (f"（上次同步 {data['last_sync']}）" if data.get("last_sync") else "（尚未同步 → monash ontrack sync）"))
        if "live" in data:
            lv = data["live"]
            print(f"在线核验：通过（认证方式 {lv['auth_method']}，项目 {lv['projects']} 个，角色 {lv['unit_roles']} 个）")
    return 0


def _cmd_doctor(args) -> int:
    st = ontrack.local_status()
    report: dict[str, Any] = {"local": st, "site": None, "live_session": None, "ok": True}
    url = st.get("base_url")
    if url:
        try:
            method = ontrack.discover_auth_method(url)
            report["site"] = {"reachable": True, **method}
        except ontrack.OnTrackError as e:
            report["site"] = {"reachable": False, "error": str(e)}
            report["ok"] = False
    else:
        report["site"] = {"reachable": False, "error": "未配置 ontrack_url"}
        report["ok"] = False

    if args.live:
        try:
            live = ontrack.auth_check(ontrack.OnTrackClient())
            report["live_session"] = {"authenticated": True, **live}
        except ontrack.OnTrackError as e:
            report["live_session"] = {"authenticated": False, "error": str(e)}
            report["ok"] = False

    if want_json(args):
        emit_json(report)
    else:
        print(f"OnTrack 站点：{url or '未配置'} -> {'可达' if (report['site'] or {}).get('reachable') else '不可达'}")
        print(f"本机凭据：{'已保存' if st.get('configured') else '未登录'}")
        if report["live_session"] is not None:
            ok = report["live_session"].get("authenticated")
            print(f"在线会话：{'有效' if ok else '失效：' + str(report['live_session'].get('error', ''))}")
    return 0 if report["ok"] else 3


def _cmd_get(args) -> int:
    cl = ontrack.OnTrackClient()
    pid = ontrack.resolve_project_id(cl, args.unit)
    if not args.task:
        archive = cl.download_unit_resources(pid)
        target = Path(args.to) if args.to else Path.cwd() / archive["filename"]
        if target.is_dir():
            target = target / archive["filename"]
        saved = ontrack.save_download(target, archive["bytes"], force=args.force)
        res = {"kind": "unit_resources", "project_id": pid, "path": str(saved), "bytes": len(archive["bytes"])}
    else:
        snap = ontrack.project_snapshot(cl, pid)
        row, td = ontrack.select_task(snap, args.task)
        if args.resources:
            if td.get("has_task_resources") is False:
                raise ontrack.OnTrackError(f"任务 {row['abbreviation']} 没有额外资源压缩包")
            dl = cl.download_task_resources(snap["unit"]["id"], td["id"])
            default_name = dl.get("filename") or f"{ontrack._safe_filename(snap['unit']['code'])}-{ontrack._safe_filename(row['abbreviation'])}-resources.zip"
            kind = "task_resources"
        else:
            if td.get("has_task_sheet") is False:
                raise ontrack.OnTrackError(f"任务 {row['abbreviation']} 没有任务书 PDF")
            dl = cl.download_task_sheet(snap["unit"]["id"], td["id"])
            default_name = f"{ontrack._safe_filename(snap['unit']['code'])}-{ontrack._safe_filename(row['abbreviation'])}.pdf"
            kind = "task_sheet"
        target = Path(args.to) if args.to else Path.cwd() / default_name
        if target.is_dir():
            target = target / default_name
        saved = ontrack.save_download(target, dl["bytes"], force=args.force)
        res = {"kind": kind, "project_id": pid, "task": row["abbreviation"], "path": str(saved), "bytes": len(dl["bytes"])}
    if want_json(args):
        emit_json(res)
    else:
        print(f"↓ {res['path']} ({res['bytes'] / 1024:.1f} KiB)")
    return 0


def _cmd_set(args) -> int:
    state = args.state
    if state not in ontrack.WRITABLE_TASK_STATES:
        raise ValueError(f"状态必须是：{', '.join(ontrack.WRITABLE_TASK_STATES)}")
    cl = ontrack.OnTrackClient()
    snap = ontrack.project_snapshot(cl, args.unit)
    row, td = ontrack.select_task(snap, args.task)
    summary = f"将 [{snap['unit']['code']}] {row['abbreviation']} ({row['name']}) 的状态从 {row['status']} 改为 {state}。"
    ok, code = _confirm_action(args, summary, {
        "project_id": snap["project"]["id"], "task": row["abbreviation"],
        "previous_status": row["status"], "next_status": state,
    })
    if not ok:
        return code
    updated = cl.update_task_state(snap["project"]["id"], td["id"], state)
    out = {
        "project_id": snap["project"]["id"],
        "task_definition_id": td["id"],
        "task": row["abbreviation"],
        "previous_status": row["status"],
        "status": updated["status"],
    }
    if want_json(args):
        emit_json(out)
    else:
        print(f"✓ {row['abbreviation']}：{row['status']} → {updated['status']}")
    return 0


def _cmd_submit(args) -> int:
    if args.comment and len(args.comment) > 4095:
        raise ValueError("提交备注不能超过 4095 个字符")
    cl = ontrack.OnTrackClient()
    snap = ontrack.project_snapshot(cl, args.unit)
    row, td = ontrack.select_task(snap, args.task)
    uploads = ontrack.prepare_uploads(args.file, td.get("upload_requirements", []))
    file_desc = ", ".join(f"{u['requirement_name']}={u['filename']} ({u['size']} B)" for u in uploads)
    summary = (
        f"准备向 [{snap['unit']['code']}] {row['abbreviation']} ({row['name']}) 提交 {len(uploads)} 个文件"
        f"（类型 {args.type}）：{file_desc}"
        + (f"；备注：{args.comment}" if args.comment else "")
    )
    ok, code = _confirm_action(args, summary, {
        "project_id": snap["project"]["id"],
        "task": row["abbreviation"],
        "previous_status": row["status"],
        "type": args.type,
        "files": [{"key": u["key"], "requirement": u["requirement_name"], "filename": u["filename"], "bytes": u["size"]} for u in uploads],
    })
    if not ok:
        return code
    updated = cl.submit_task(
        snap["project"]["id"],
        td["id"],
        uploads,
        submission_type=args.type,
        comment=args.comment,
        accept_tii_eula=bool(args.accept_tii_eula),
    )
    out = {
        "project_id": snap["project"]["id"],
        "task_definition_id": td["id"],
        "task": row["abbreviation"],
        "previous_status": row["status"],
        "status": updated["status"],
        "submitted_files": [u["filename"] for u in uploads],
    }
    if want_json(args):
        emit_json(out)
    else:
        print(f"✓ 已提交 {row['abbreviation']}：{row['status']} → {updated['status']}")
    return 0


def _cmd_chats(args) -> int:
    cl = ontrack.OnTrackClient()
    if not args.task:
        rows = ontrack.list_chats(cl, args.unit)
        if want_json(args):
            emit_json(rows)
        else:
            table(rows, [
                ("task", "任务", 8),
                ("name", "名称", 36),
                ("status_label", "状态", 18),
                ("unread_comments", "未读", 6),
            ])
        return 0
    snap = ontrack.project_snapshot(cl, args.unit)
    row, td = ontrack.select_task(snap, args.task)
    summary = (
        f"读取 [{snap['unit']['code']}] {row['abbreviation']} 的导师/学生评论历史；"
        "注意：OnTrack 服务端会在拉取后自动将非讨论类评论标为已读。"
    )
    ok, code = _confirm_action(args, summary, {
        "project_id": snap["project"]["id"], "task": row["abbreviation"], "marks_read": True,
    })
    if not ok:
        return code
    comments = cl.task_comments(snap["project"]["id"], td["id"])
    if want_json(args):
        emit_json(comments)
    else:
        if not comments:
            print("（还没有评论）")
        for c in comments:
            author = f"{c['author']['first_name']} {c['author']['last_name']}".strip()
            flag = "● " if c.get("is_new") else "  "
            print(f"{flag}{c['created_at']}  {author}：{c['comment']}")
    return 0


def _cmd_chat_send(args) -> int:
    msg = (args.message or "").strip()
    if not msg:
        raise ValueError("消息内容不能为空")
    if len(msg) > 4095:
        raise ValueError("消息内容不能超过 4095 个字符")
    cl = ontrack.OnTrackClient()
    snap = ontrack.project_snapshot(cl, args.unit)
    row, td = ontrack.select_task(snap, args.task)
    summary = f"向 [{snap['unit']['code']}] {row['abbreviation']} 发送评论：{msg!r}"
    ok, code = _confirm_action(args, summary, {
        "project_id": snap["project"]["id"], "task": row["abbreviation"], "message": msg,
    })
    if not ok:
        return code
    posted = cl.add_task_comment(snap["project"]["id"], td["id"], msg)
    out = {
        "project_id": snap["project"]["id"],
        "task_definition_id": td["id"],
        "task": row["abbreviation"],
        "comment_id": posted["id"],
        "message": posted["comment"],
        "created_at": posted["created_at"],
    }
    if want_json(args):
        emit_json(out)
    else:
        print(f"✓ 已发送到 {row['abbreviation']}（comment #{posted['id']}）")
    return 0


def add_commands(sub) -> None:
    def cmd(name: str, help_text: str, *, aliases: tuple[str, ...] = (), json_flag: bool = True):
        p = sub.add_parser(name, aliases=list(aliases), help=help_text)
        if json_flag:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--json", action="store_true", help="输出 JSON（管道里默认就是）")
            g.add_argument("--text", action="store_true", help="输出文本（终端里默认就是）")
        p.set_defaults(fn=run)
        return p

    def add_confirm(p):
        p.add_argument("--dry-run", action="store_true", help="只显示将执行的操作，不发请求")
        p.add_argument("--confirm", action="store_true", help="确认执行写操作")
        p.add_argument("--yes", "-y", action="store_true", help="同 --confirm")

    p = cmd("login", "登录 OnTrack / Doubtfire（默认复用本机专用浏览器配置）")
    p.add_argument("--url", help="OnTrack 站点地址（HTTPS），如 https://ontrack.example.edu")
    p.add_argument("--window", action="store_true", help="直接开登录窗口，不先试后台自动续期")
    p.add_argument("--paste", action="store_true", help="粘贴 Cookie / cURL / access-token JSON 或 refresh_token 登录")
    p.add_argument("--username", help="配合 --paste 粘贴裸 refresh_token 时指定用户名")
    p.add_argument("--allow-write", action="store_true", help="同时在 MCP 中开启 OnTrack 受控写操作（默认只读）")
    p.add_argument("--timeout", type=int, default=300, help="等待登录的最长秒数")

    p = cmd("status", "查看 OnTrack 登录与缓存状态")
    p.add_argument("--local", action="store_true", help="只看本地状态，不联网核验")

    cmd("renew", "静默续期当前 OnTrack 会话")
    cmd("logout", "清除本机保存的 OnTrack 会话")
    cmd("user", "显示当前登录的 OnTrack 用户信息")

    p = cmd("sync", "同步活跃课程与任务快照到本地缓存（供 monash todo / semester 使用）")
    p.add_argument("--sheets", action="store_true", help="同时把每个任务的任务书 PDF 缓存到本地")

    p = cmd("courses", "列出 OnTrack 课程（项目）", aliases=("units", "projects"))
    p.add_argument("--include-inactive", action="store_true", help="包含往期学期的课程")

    p = cmd("show", "查看一门课的任务进度与截止日期")
    p.add_argument("unit", help="课程号、课程名或项目 ID")

    p = cmd("tasks", "列出一门课的任务")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("--status", nargs="+", help="按原始状态筛选，如 not_started fix_and_resubmit")

    p = cmd("task", "查看单个任务详情与提交要求")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", help="任务编号（如 1.1P）或 task_definition_id")

    p = cmd("read", "将任务书 PDF 转为 Markdown 输出")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", help="任务编号（如 1.1P）或 task_definition_id")
    p.add_argument("--no-cache", action="store_true", help="不把 PDF 缓存到本地目录")

    p = cmd("get", "下载课程资源 ZIP、任务书 PDF 或任务资源包")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", nargs="?", help="任务编号；省略则下载整门课资源 ZIP")
    p.add_argument("--resources", action="store_true", help="下载任务附件资源（而不是任务书 PDF）")
    p.add_argument("--to", help="保存路径（文件或目录）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")

    p = cmd("set", "修改任务进度状态（not_started / working_on_it / need_help）")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", help="任务编号")
    p.add_argument("state", choices=list(ontrack.WRITABLE_TASK_STATES), help="目标状态")
    add_confirm(p)

    p = cmd("submit", "提交任务文件（默认只预览，确认后加 --confirm 执行）")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", help="任务编号")
    p.add_argument("--file", nargs="+", required=True, help="按提交要求顺序传入本地文件路径")
    p.add_argument("--type", choices=list(ontrack.SUBMISSION_TYPES), default="ready_for_feedback", help="提交类型")
    p.add_argument("--comment", help="附带的提交备注")
    p.add_argument("--accept-tii-eula", action="store_true", help="确认同意 Turnitin EULA")
    add_confirm(p)

    p = cmd("chats", "查看未读评论数（只读），或读取某任务评论历史（会标已读，需确认）")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", nargs="?", help="任务编号；省略则只列各任务未读数")
    add_confirm(p)

    p = cmd("chat-send", "向指定任务发送导师/学生评论")
    p.add_argument("unit", help="课程号、课程名或项目 ID")
    p.add_argument("task", help="任务编号")
    p.add_argument("--message", required=True, help="要发送的完整消息内容")
    add_confirm(p)

    p = cmd("roles", "列出自己的教学角色（如 Tutor / Convenor）")
    p.add_argument("--all", action="store_true", help="包含往期学期的角色")

    p = cmd("doctor", "诊断 OnTrack 站点连接、本机凭据与在线会话")
    p.add_argument("--live", action="store_true", help="同时联网校验受保护接口")
