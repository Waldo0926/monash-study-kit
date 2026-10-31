"""Gmail commands: durable mirror and optional mailbox management."""
from __future__ import annotations

import argparse
import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from . import gmail
from .paths import save_settings


def run(args):
    try:
        return _run(args)
    except gmail.GmailError as e:
        # 没装 [gmail] 时 main 会把它当成“连不上”，说清楚是少装了东西
        if isinstance(e.__cause__, ImportError):
            print(f"{e}\n装好后重新运行这条命令。", file=sys.stderr)
            return 1
        raise


def _run(args):
    if args.gcmd == "login":
        # 登录后就全部能用：默认连起草、发草稿、改标签的权限一起要。每次操作仍然先预览、确认才执行
        manage = not args.read_only
        result = gmail.login(Path(args.client), manage=manage)
        save_settings({"gmail_enabled": True, "gmail_manage_enabled": manage})
    else:
        store = gmail.Store()
        try:
            client = gmail.Client()
            if args.gcmd == "status":
                result = store.status()
            elif args.gcmd == "sync":
                result = _sync(client, store, args)
            elif args.gcmd == "todo":
                result = store.inbox_todo(days=args.days, limit=args.limit)
            elif args.gcmd == "search":
                result = store.search(args.query, limit=args.limit, offset=args.offset)
            elif args.gcmd == "thread":
                result = store.read_thread(args.thread_id, offset=args.offset)
            elif args.gcmd == "attachment":
                result = gmail.cache_attachment(client, store, args.message_id, args.part_id)
            elif args.gcmd == "draft":
                raw = Path(args.file).read_bytes()
                message = BytesParser(policy=policy.default).parsebytes(raw)
                result = {"action": "create_draft", "to": message.get_all("To", []),
                          "cc": message.get_all("Cc", []), "bcc": message.get_all("Bcc", []),
                          "subject": message.get("Subject", ""), "bytes": len(raw),
                          "body": (message.get_body(preferencelist=("plain", "html")) or message).get_content(),
                          "attachments": [part.get_filename() for part in message.iter_attachments()],
                          "committed": False}
                if args.confirm:
                    result = {"action": "create_draft", "committed": True,
                              "result": client.create_draft(raw)}
            elif args.gcmd == "send-draft":
                draft = client.get(f"drafts/{args.draft_id}", format="full")
                content = gmail.message_content(draft["message"])
                headers = {h["name"].lower(): h["value"] for h in draft["message"].get("payload", {}).get("headers", [])}
                result = {"action": "send_draft", "draft_id": args.draft_id,
                          "to": content["recipients"], "cc": headers.get("cc", ""), "bcc": headers.get("bcc", ""),
                          "subject": content["subject"], "body": content["body"], "committed": False}
                if args.confirm:
                    result = {"action": "send_draft", "committed": True,
                              "result": client.send_draft(args.draft_id)}
            elif args.gcmd == "labels":
                result = {"action": "modify_labels", "message_id": args.message_id,
                          "add": args.add, "remove": args.remove, "committed": False}
                if args.confirm:
                    result = {**result, "committed": True,
                              "result": client.modify_labels(args.message_id, add=args.add, remove=args.remove)}
            else:
                raise ValueError("Unknown Gmail command")
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _sync(client, store, args):
    # 每页和它的游标一起提交：中途出错时前面的页已经存好，再运行一次会从出错那页接着下。
    try:
        result = gmail.sync(client, store, query=args.query, page_limit=args.pages)
    except gmail.GmailError:
        print("同步中断了。已经下载的页都保存好了，再运行一次 monash gmail sync 会从断点继续。", file=sys.stderr)
        raise
    if result["status"] == "more_pending":
        print("还有没下载完的邮件，再运行一次 monash gmail sync（或加大 --pages）。", file=sys.stderr)
    elif result["status"] == "resync_required":
        print("Gmail 的增量记录过期了，再运行一次 monash gmail sync 会重新下载所选范围。", file=sys.stderr)
    return result


def add_commands(sub):
    def command(name, help_text):
        parser = sub.add_parser(name, help=help_text)
        parser.set_defaults(fn=run)
        return parser

    p = command("login", "用系统浏览器授权 Gmail；凭据只保存在本机")
    p.add_argument("--client", required=True, help="Google OAuth Desktop app client JSON")
    p.add_argument("--read-only", action="store_true",
                   help="只要读邮件的权限，不让 AI 起草、发草稿或改标签（默认这些都可以，每次都要你确认）")
    p.add_argument("--manage", action="store_true", help=argparse.SUPPRESS)   # 以前的写法，现在是默认
    command("status", "查看本地 Gmail 镜像的覆盖范围和同步状态")
    p = command("sync", "分页下载正文及完整消息 payload；每页单独保存，中断后再运行会接着下")
    p.add_argument("--query", default="newer_than:90d", help="初次同步 Gmail 搜索范围；空字符串为全部")
    p.add_argument("--pages", type=int, default=5, help="本次最多处理多少页；未完成时再次运行")
    p = command("todo", "邮件待办：最近几天收件箱里没读的学校邮件，去掉推广和社交类（只读本地镜像）")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=10)
    p = command("search", "离线全文搜索本地已下载的邮件，不是 Gmail 搜索语法；结果带日期、未读、星标、分类和附件数")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p = command("thread", "读取已保存的对话；可能不含同步范围外的消息")
    p.add_argument("thread_id")
    p.add_argument("--offset", type=int, default=0)
    p = command("attachment", "下载指定附件并保存到私有镜像目录")
    p.add_argument("message_id")
    p.add_argument("part_id", help="thread 返回的 MIME part_id")
    p = command("draft", "预览 .eml；--confirm 才在 Gmail 创建草稿")
    p.add_argument("--file", required=True)
    p.add_argument("--confirm", action="store_true")
    p = command("send-draft", "预览已有草稿；--confirm 才发出邮件")
    p.add_argument("draft_id")
    p.add_argument("--confirm", action="store_true")
    p = command("labels", "预览标签修改；--confirm 才操作邮箱")
    p.add_argument("message_id")
    p.add_argument("--add", action="append", default=[])
    p.add_argument("--remove", action="append", default=[])
    p.add_argument("--confirm", action="store_true")
