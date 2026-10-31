"""考勤命令：登录门户、看状态、读取最近的班次和签到情况、找签到码。

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
        elif args.acmd == "overview":
            result = attendance.overview(args.days)
        elif args.acmd == "probe":
            from . import attendance_http
            result = attendance_http.probe()
            print("下面只有数量和对不对得上，没有课程、时间、链接或 cookie，可以直接发给维护者。", file=sys.stderr)
        elif args.acmd == "codes":
            from .paths import load_settings
            result = attendance.find_codes(args.days, gmail_allowed=bool(load_settings().get("gmail_enabled")))
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
    p = command("overview", "看已保存的班次：已签到、还没签、还没到时间（不联网）")
    p.add_argument("--days", type=int, default=14, help="往前看几天")
    command("probe", "检查能不能不开浏览器读考勤：两种读法对照一遍，只输出数量（要先登录，约一两分钟）")
    p = command("codes", "从已同步的 Moodle、Ed 和学校邮件里找还没签到的班次的签到码（只是候选，要自己核对）")
    p.add_argument("--days", type=int, default=7, help="找最近几天的")
