"""Gmail commands: durable mirror and optional mailbox management."""
from __future__ import annotations

import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

from . import gmail
from .paths import save_settings


def run(args):
    if args.gcmd == "login":
        result = gmail.login(Path(args.client), manage=args.manage)
        save_settings({"gmail_enabled": True})
    else:
        store = gmail.Store()
        try:
            client = gmail.Client()
            if args.gcmd == "status":
                result = store.status()
            elif args.gcmd == "sync":
                result = gmail.sync(client, store, query=args.query, page_limit=args.pages)
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


def add_commands(sub):
    def command(name, help_text):
        parser = sub.add_parser(name, help=help_text)
        parser.set_defaults(fn=run)
        return parser

    p = command("login", "用系统浏览器授权 Gmail；凭据只保存在本机")
    p.add_argument("--client", required=True, help="Google OAuth Desktop app client JSON")
    p.add_argument("--manage", action="store_true", help="可选：授权草稿、发信及标签修改")
    command("status", "查看本地 Gmail 镜像的覆盖范围和同步状态")
    p = command("sync", "分页下载正文及完整消息 payload；失败可重试")
    p.add_argument("--query", default="newer_than:90d", help="初次同步 Gmail 搜索范围；空字符串为全部")
    p.add_argument("--pages", type=int, default=5, help="本次最多处理多少页；未完成时再次运行")
    p = command("search", "离线全文搜索本地已下载的邮件，不是 Gmail 搜索语法")
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
