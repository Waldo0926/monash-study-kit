"""课程资料模式的集中命令和 MCP 工具清单，新增命令默认不放行。"""
from __future__ import annotations

import re
import urllib.parse

from . import paths

# 空集合表示只允许这一条顶层命令；子命令使用显式白名单。
COMMANDS = {
    "setup": {None}, "login": {"moodle", "ed"}, "google": {"login", "status", "logout"},
    "changes": set(), "sync": set(), "export": set(), "courses": set(), "status": set(), "doctor": set(),
    "open": set(), "library": set(), "config": set(), "grep": set(), "help": set(), "mcp": set(),
    "profile": {"list", "remove"}, "ed": {"export", "lessons", "read", "quiz"}, "moodle": {"ls", "get"},
}
SUBCOMMANDS = {"setup": "what", "login": "what", "google": "google_cmd", "profile": "profile_cmd",
               "ed": "ecmd", "moodle": "mcmd"}
MCP_TOOLS = frozenset({"list_files", "read_file", "search_content", "get_ed_lessons", "export_ed_lessons"})
LOCAL_ONLY = "命名 profile 的会话和令牌只能留在本机，不能交给远程服务或 AI 软件配置"


def command_error(args, settings: dict) -> str | None:
    """在调用处理函数之前统一拒绝；默认账号关闭此模式时保持原行为。"""
    cmd = args.cmd
    named = not paths.is_default_profile()
    if named:
        if cmd in {"remote", "connect", "move"} or cmd == "setup" and getattr(args, "what", None) == "claude":
            return LOCAL_ONLY + ("；profile 会随默认数据目录一起移动" if cmd == "move" else "")
        if cmd == "profile":
            return "请在默认账号下运行 monash profile，不能在命名 profile 内管理其他账号"
        if cmd == "config" and getattr(args, "key", None) == "materials_only" and getattr(args, "value", None) is not None:
            return "命名 profile 强制启用只导课程资料模式，不能修改 materials_only"
        if cmd == "config" and getattr(args, "key", None) == "files_dir" and getattr(args, "value", None) is not None:
            return "命名 profile 的课件目录固定在本账号目录内，不能修改 files_dir"
    if not settings.get("materials_only") or cmd is None:
        return None
    sub = getattr(args, SUBCOMMANDS.get(cmd, "_unused"), None)
    allowed = COMMANDS.get(cmd)
    if allowed is None or allowed and sub not in allowed:
        command = " ".join(str(v) for v in (cmd, sub) if v is not None)
        return f"只导课程资料模式下不能用：{command}"
    if cmd == "config" and getattr(args, "key", None) == "materials_only" and getattr(args, "value", None) is not None:
        return "只导课程资料模式下不能用：config materials_only；当前模式下只能查看此设置"
    if cmd == "moodle" and sub == "get":
        ref = getattr(args, "ref", "")
        if ref.startswith("http"):
            from .moodlelib import HOST
            try:
                u = urllib.parse.urlsplit(ref)
                port = u.port
            except ValueError:
                return "只导课程资料模式下不能用：moodle get 的课件地址无效"
            activity = re.fullmatch(r"/mod/(resource|folder|url)/view\.php", u.path)
            file = re.search(r"/pluginfile\.php/\d+/(?:mod_(?:resource|folder|page|book)/content/|mod_assign/intro(?:attachment)?/)", u.path)
            if u.scheme != "https" or u.hostname != HOST or u.username or port or not (activity or file):
                return "只导课程资料模式下不能用：moodle get 只能下载课程资源或课件文件"
    return None
