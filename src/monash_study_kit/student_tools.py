"""本地 MCP 的可选学生服务工具：Gmail 和考勤。

工具的实现在 student_services 里，私人部署和这里用的是同一份。这里只做两件事：
按 settings 决定注册哪些，再把本地设置换成 student_services 认的权限（scope）。

  * gmail_enabled              → 只读邮件工具（gmail_tools，读本地镜像），加读附件
  * gmail_manage_enabled       → gmail_prepare_action + execute_student_action
  * attendance_enabled         → 考勤的读取、同步、找签到码、导出，加 student_job_status
  * attendance_submit_enabled  → attendance_prepare_submit + execute_student_action

登录前什么都不注册；monash gmail login、monash attendance login 成功后，对应的四个设置
都会打开（Gmail 用 --read-only 登录时只开读）。写操作每次都先返回完整预览和一次性凭据，
用户确认这一项后才能 execute_student_action。

远程 MCP 用的是另一套名字（student_services.TOOLS 加上 REMOTE_EXTRA），按每个客户端
配对时勾选的功能给，再和服务器自己的设置取交集：服务器没开的，哪个客户端都拿不到。
"""
from __future__ import annotations

import json
import traceback

from . import gmail, gmail_tools, service_jobs
from . import student_services as S
from .gmail import GmailAuthError

OWNER = "local"   # 本机只有一个用户，确认凭据记在这个名下
GMAIL_READ = ("gmail_read_attachment",)   # 附件不在镜像里时要下载，所以不放在 gmail_tools
# 同步、找码、导出要开浏览器或读很多文件，交给后台任务，返回 job_id 再用 student_job_status 查
ATTENDANCE_READ = ("attendance_status", "attendance_sync", "attendance_overview", "attendance_find_codes",
                   "attendance_export")
JOB_STATUS = "student_job_status"
GMAIL_WRITE = ("gmail_prepare_action",)
ATTENDANCE_WRITE = ("attendance_prepare_submit",)
EXECUTE = "execute_student_action"
# 远程 MCP 按这个名单整组拒绝
NAMES = frozenset(gmail_tools.TOOLS) | frozenset(GMAIL_READ + ATTENDANCE_READ + GMAIL_WRITE + ATTENDANCE_WRITE
                                                 + (EXECUTE, JOB_STATUS))


def scopes(settings: dict) -> set[str]:
    out = set()
    if settings.get("gmail_enabled"):
        out.add("gmail.read")
        if settings.get("gmail_manage_enabled"):
            out.add("gmail.manage")
    if settings.get("attendance_enabled"):
        out.add("attendance.read")
        if settings.get("attendance_submit_enabled"):
            out.add("attendance.submit")
    return out


def _require_manage(name: str) -> None:
    """只读授权下准备邮箱操作，执行时 Gmail 才会拒绝，那时凭据已经记成“结果未知”。先查好。"""
    if name == "gmail_prepare_action" and gmail.MANAGE_SCOPE not in (gmail.Client()._credentials().scopes or []):
        raise PermissionError("现在的 Gmail 授权只能读，邮箱操作要写权限。请用户在终端运行 "
                              "monash gmail login（不加 --read-only），再重启 AI 软件。")


def _runner(name: str, granted: set[str]):
    def run(arguments):
        try:
            _require_manage(name)
            if name in REMOTE_EXTRA:
                handler, _, params, _, _ = REMOTE_EXTRA[name]
                _check(params, arguments)
                return handler(arguments, {"scopes": granted, "owner": OWNER})
            return S.call(name, arguments, granted, OWNER)
        except GmailAuthError as e:
            raise PermissionError(f"Gmail 授权失效或权限不够（{e}）。请用户在终端运行 "
                                  "monash gmail login。") from e
        except RuntimeError as e:
            from .attendance import AttendanceAuthError
            if isinstance(e, AttendanceAuthError):
                raise PermissionError(f"考勤门户登录过期或还没登录（{e}）。请用户在终端运行 monash attendance login。") from e
            raise
    return run


def tools(settings: dict) -> dict:
    """按设置挑出要注册的工具，格式和 mcp_server.TOOLS 一样。"""
    granted = scopes(settings)
    out = dict(gmail_tools.TOOLS) if "gmail.read" in granted else {}
    names = list(GMAIL_READ) if "gmail.read" in granted else []
    if "attendance.read" in granted:
        names += ATTENDANCE_READ
    if "gmail.manage" in granted:
        names += GMAIL_WRITE
    if "attendance.submit" in granted:
        names += ATTENDANCE_WRITE
    if granted & {"gmail.manage", "attendance.submit"}:
        names.append(EXECUTE)
    if "attendance.read" in granted:
        names.append(JOB_STATUS)
    for name in names:
        _, desc, params, _, read_only = REMOTE_EXTRA[name] if name in REMOTE_EXTRA else S.TOOLS[name]
        out[name] = (_runner(name, granted), desc, params, read_only)
    return out


# ---------------------------------------------------------------- 远程

def _job_status(arguments, ctx):
    job = service_jobs.status(ctx["owner"], arguments["job_id"])
    if not all(S.allowed(scope, ctx["scopes"]) for scope in job["required_scopes"]):
        raise PermissionError("This client is no longer authorized for this job")
    return job


# 格式同 student_services.TOOLS：handler, 说明, 参数, 需要的权限, 是否只读
REMOTE_EXTRA = {
    "gmail_inbox_todo": (
        lambda a, c: S.with_mail(lambda cl, st: st.inbox_todo(days=a.get("days", 7), limit=a.get("limit", 10))),
        "邮件待办：最近几天收件箱里还没读的学校邮件，已去掉推广和社交类；用户问“邮箱里有什么要处理的”先用这个。"
        "读的是后台同步的副本，as_of 是上次同步时间。",
        {"days": (S.I, False), "limit": (S.I, False)},
        "gmail.read",
        True,
    ),
    "student_job_status": (
        _job_status,
        "查 gmail_sync、attendance_sync 等返回的 job_id 的进度和结果。",
        {"job_id": (S.S, True)},
        "job",
        True,
    ),
}
REMOTE_NAMES = frozenset(S.TOOLS) | frozenset(REMOTE_EXTRA)
# 配对页上让用户勾选的功能（写操作那两项默认不勾）
REMOTE_CHOICES = {
    "gmail.read": "学校邮箱（只读）：查邮件、邮件待办、读附件，不会改邮箱",
    "gmail.manage": "邮箱操作：起草、发送草稿、改标签。每次都先给你看预览，你确认后才执行",
    "attendance.read": "考勤（只读）：查班次和签到状态",
    "attendance.submit": "签到提交：你说自己到课了才准备，每次都要你确认",
}


def remote_scopes(token_scopes, settings: dict) -> set[str]:
    """客户端被授权的功能，再去掉服务器设置里没开的。"""
    granted = set(token_scopes) & scopes(settings)
    if "gmail.manage" in granted:
        granted.add("gmail.read")
    if "attendance.submit" in granted:
        granted.add("attendance.read")
    return granted


def _allowed(name: str, granted: set[str]) -> bool:
    scope = REMOTE_EXTRA[name][3] if name in REMOTE_EXTRA else S.TOOLS[name][3]
    return bool(granted) if scope == "job" else S.allowed(scope, granted)


def remote_tool_list(granted: set[str]) -> list[dict]:
    out = S.tool_list(granted | {"monash"}) if granted else []
    for name, (_, desc, params, scope, read_only) in REMOTE_EXTRA.items():
        if _allowed(name, granted):
            required = scope if scope in granted else min(granted)
            out.append({
                "name": name, "description": desc,
                "inputSchema": {"type": "object", "properties": {k: {"type": v[0]} for k, v in params.items()},
                                "required": [k for k, v in params.items() if v[1]], "additionalProperties": False},
                "annotations": {"readOnlyHint": read_only},
                "securitySchemes": [{"type": "oauth2", "scopes": ["monash", required]}],
            })
    return out


def remote_allowed(name: str, granted: set[str]) -> bool:
    return name in REMOTE_NAMES and _allowed(name, granted)


def remote_call(name: str, arguments: dict, granted: set[str], owner: str) -> dict:
    """在远程执行一个学生服务工具，返回 MCP 的 tools/call 结果。错误也变成 isError，不抛出去。"""
    from . import mcp_server as M
    try:
        _require_manage(name)
        if name in REMOTE_EXTRA:
            handler, _, params, _, _ = REMOTE_EXTRA[name]
            _check(params, arguments)
            data = handler(arguments, {"scopes": granted, "owner": owner})
        else:
            data = S.call(name, arguments, granted, owner)
    except GmailAuthError as e:
        return _error(f"Gmail 授权失效或权限不够（{e}）。请用户在服务器的终端重新运行 monash gmail login。")
    except PermissionError as e:
        return _error(str(e).replace("在终端", "在服务器的终端").replace("，再重启 AI 软件", ""))
    except (ValueError, LookupError) as e:
        return _error(str(e))
    except Exception as e:  # noqa: BLE001
        from .attendance import AttendanceAuthError
        if isinstance(e, AttendanceAuthError):
            return _error(f"考勤门户登录过期（{e}）。请用户在服务器的终端运行 monash attendance login。")
        M.log(f"远程工具 {name} 出错：\n" + traceback.format_exc())
        hint = "结果未知，不要重试，先去核对实际状态。" if name == EXECUTE else ""
        return _error(f"出错了：{e}。{hint}")
    return {"content": [{"type": "text", "text": M._clip(json.dumps(data, ensure_ascii=False), M.MAX_OUT)}]}


def _check(params: dict, arguments: dict) -> None:
    types = {S.S: str, S.I: int, S.B: bool}
    for key, (kind, required) in params.items():
        if required and key not in arguments:
            raise ValueError("Missing argument: " + key)
        if key in arguments and type(arguments[key]) is not types[kind]:
            raise ValueError("Invalid argument type: " + key)
    if arguments.keys() - params.keys():
        raise ValueError("Unknown arguments")


def _error(text: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def remote_hint(granted: set[str]) -> str:
    out = ""
    if "gmail.read" in granted:
        out += ("本服务连着用户的 Monash 学校邮箱。用户问“我的邮箱/邮件/有什么要处理的/学校发的邮件”时，"
                "优先用 gmail_inbox_todo、gmail_search（实时）或 gmail_cache_search（离线），不要改用其他通用 Gmail "
                "连接器，除非用户明确说的是别的邮箱。")
    if "attendance.read" in granted:
        out += "问签到/考勤用 attendance_overview；签到码用 attendance_find_codes。"
    if granted:
        out += "同步类工具返回 job_id，用 student_job_status 查结果。"
    if granted & {"gmail.manage", "attendance.submit"}:
        out += ("写操作先用 prepare 工具拿完整预览原样给用户看，用户明确确认这一项后才调 execute_student_action"
                "（user_confirmed=true）；不能自己替用户确认，结果未知时不要重试。")
    return out
