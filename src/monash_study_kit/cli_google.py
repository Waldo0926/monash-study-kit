"""Google 课程文件的独立授权命令。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import google_export as G


def run(args):
    try:
        if args.google_cmd == "login":
            client = Path(args.client).expanduser() if args.client else Path(__file__).with_name("google_client.json")
            result = G.login(client)
        elif args.google_cmd == "logout":
            result = G.logout()
        else:
            result = G.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (G.GoogleError, OSError, ValueError) as e:
        print(str(e) if isinstance(e, G.GoogleError) else "Google 授权文件无法读取，请检查客户端 JSON", file=sys.stderr)
        return 1


def add_commands(sub):
    for name, help_text in (("login", "授权 Google Drive 只读导出"), ("status", "查看授权和 scope"), ("logout", "吊销授权并删除令牌")):
        parser = sub.add_parser(name, help=help_text)
        parser.set_defaults(fn=run)
        if name == "login":
            parser.add_argument("--client", metavar="JSON", help="Google 桌面应用客户端 JSON")
