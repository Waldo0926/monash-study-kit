"""考勤命令：登录门户、看状态、读取最近的班次和签到情况。

要先装 [attendance]（playwright），再运行一次 python -m playwright install chromium。
attendance 模块只在这里用到时才导入：它一导入就要读时区库，没装考勤的人不该受影响。
"""
from __future__ import annotations

import json
import sys

from .paths import save_settings


def run(args):
    from .cli import EXIT_AUTH, EXIT_NETWORK
    from . import attendance
    try:
        if args.acmd == "login":
            result = attendance.login(timeout=args.timeout)
            save_settings({"attendance_enabled": True})
        elif args.acmd == "status":
            result = attendance.status()
        elif args.acmd == "sync":
            result = attendance.sync(args.days)
        else:
            raise ValueError("Unknown attendance command")
    except attendance.AttendanceAuthError as e:
        print(f"{e}\n考勤门户要重新登录：monash attendance login", file=sys.stderr)
        return EXIT_AUTH
    except RuntimeError as e:      # 没装 playwright，或者门户页面没按预期加载
        print(f"考勤出错：{e}", file=sys.stderr)
        return EXIT_NETWORK
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def add_commands(sub):
    def command(name, help_text):
        parser = sub.add_parser(name, help=help_text)
        parser.set_defaults(fn=run)
        return parser

    p = command("login", "打开浏览器登录考勤门户（Okta + MFA），只保存门户的登录状态，不保存密码")
    p.add_argument("--timeout", type=int, default=600, help="等你登录完的最长秒数")
    command("status", "登录状态、上次读取时间和已保存的班次数")
    p = command("sync", "读取最近几天和本周还没到的班次及签到状态，保存到本地")
    p.add_argument("--days", type=int, default=7, help="往前读几天（1 到 14）")
