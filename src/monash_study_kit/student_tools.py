"""本地 MCP 的可选学生服务工具：Gmail 和考勤。

工具的实现在 student_services 里，私人部署和这里用的是同一份。这里只做两件事：
按 settings 决定注册哪些，再把本地设置换成 student_services 认的权限（scope）。

  * gmail_enabled              → 只读邮件工具（gmail_tools，读本地镜像）
  * gmail_manage_enabled       → gmail_prepare_action + execute_student_action
  * attendance_enabled         → attendance_status、attendance_overview
  * attendance_submit_enabled  → attendance_prepare_submit + execute_student_action

写操作默认一个都不注册。开了以后也是先返回完整预览和一次性凭据，用户确认这一项后
才能 execute_student_action。远程 MCP 不提供这里的任何工具，见 remote.py。
"""
from __future__ import annotations

from . import gmail_tools
from . import student_services as S
from .gmail import GmailAuthError

OWNER = "local"   # 本机只有一个用户，确认凭据记在这个名下
ATTENDANCE_READ = ("attendance_status", "attendance_overview")
GMAIL_WRITE = ("gmail_prepare_action",)
ATTENDANCE_WRITE = ("attendance_prepare_submit",)
EXECUTE = "execute_student_action"
# 远程 MCP 按这个名单整组拒绝
NAMES = frozenset(gmail_tools.TOOLS) | frozenset(ATTENDANCE_READ + GMAIL_WRITE + ATTENDANCE_WRITE + (EXECUTE,))


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


def _runner(name: str, granted: set[str]):
    def run(arguments):
        try:
            return S.call(name, arguments, granted, OWNER)
        except GmailAuthError as e:
            raise PermissionError(f"Gmail 授权失效或权限不够（{e}）。请用户在终端运行 "
                                  "monash gmail login --client <OAuth 客户端 JSON>，写操作要加 --manage。") from e
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
    names = []
    if "attendance.read" in granted:
        names += ATTENDANCE_READ
    if "gmail.manage" in granted:
        names += GMAIL_WRITE
    if "attendance.submit" in granted:
        names += ATTENDANCE_WRITE
    if granted & {"gmail.manage", "attendance.submit"}:
        names.append(EXECUTE)
    for name in names:
        _, desc, params, _, read_only = S.TOOLS[name]
        out[name] = (_runner(name, granted), desc, params, read_only)
    return out
