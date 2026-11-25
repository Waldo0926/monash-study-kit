"""Shared tool handlers used by personal and self-hosted student MCP deployments."""

import base64
import hashlib
from email.message import EmailMessage

from . import gmail, service_actions, service_jobs


def with_mail(fn):
    store = gmail.Store()
    try:
        return fn(gmail.Client(), store)
    finally:
        store.close()


def mail_search(a, ctx):
    return with_mail(
        lambda client, store: gmail.live_search(
            client, store, a.get("query", ""), limit=a.get("limit", 20), page_token=a.get("page_token")
        )
    )


def mail_thread(a, ctx):
    def read(client, store):
        if a.get("refresh", True):
            gmail.live_thread(client, store, a["thread_id"])
        return store.read_thread(a["thread_id"], offset=a.get("offset", 0), max_chars=a.get("max_chars", 20000))

    return with_mail(read)


def mail_prepare(a, ctx):
    operation = a["operation"]
    client = gmail.Client()
    account = client.get("profile")["emailAddress"]
    if operation == "create_draft":
        message = EmailMessage()
        thread_id = None
        if a.get("reply_to_message_id"):
            parent = client.get(f"messages/{a['reply_to_message_id']}", format="full")
            headers = {h["name"].lower(): h["value"] for h in parent.get("payload", {}).get("headers", [])}
            original_id = headers.get("message-id")
            if not original_id:
                raise ValueError("The original message has no Message-ID; reply in Gmail instead")
            message["In-Reply-To"] = original_id
            message["References"] = (headers.get("references", "") + " " + original_id).strip()
            thread_id = parent["threadId"]
        for header, key in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Subject", "subject")):
            if a.get(key):
                message[header] = a[key]  # EmailMessage rejects header injection.
        if not a.get("to"):
            raise ValueError("A recipient is required")
        message.set_content(a.get("body", ""))
        payload = {
            "account": account,
            "to": a["to"],
            "cc": a.get("cc", ""),
            "bcc": a.get("bcc", ""),
            "subject": a.get("subject", ""),
            "body": a.get("body", ""),
            "thread_id": thread_id,
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode(),
        }
    elif operation == "send_draft":
        draft = client.get(f"drafts/{a['draft_id']}", format="raw")
        raw = draft["message"]["raw"]
        from email import policy
        from email.parser import BytesParser

        message = BytesParser(policy=policy.default).parsebytes(gmail._decode(raw))
        body = (message.get_body(preferencelist=("plain", "html")) or message).get_content()
        payload = {
            "account": account,
            "draft_id": a["draft_id"],
            "raw_sha256": hashlib.sha256(gmail._decode(raw)).hexdigest(),
            "to": message.get("To", ""),
            "cc": message.get("Cc", ""),
            "bcc": message.get("Bcc", ""),
            "subject": message.get("Subject", ""),
            "body": body,
            "attachments": [p.get_filename() for p in message.iter_attachments()],
        }
    elif operation == "labels":
        if bool(a.get("message_id")) == bool(a.get("thread_id")):
            raise ValueError("Give exactly one of message_id or thread_id")
        names = _label_ids(client)
        resolve = lambda text: [_resolve_label(v.strip(), names) for v in text.split(",") if v.strip()]
        payload = {"account": account, "add": resolve(a.get("add", "")), "remove": resolve(a.get("remove", ""))}
        if not payload["add"] and not payload["remove"]:
            raise ValueError("Nothing to change: give add and/or remove")
        payload["changes"] = [f"+{names.get(i, i)}" for i in payload["add"]] + [
            f"-{names.get(i, i)}" for i in payload["remove"]
        ]
        if a.get("thread_id"):
            thread = client.get(f"threads/{a['thread_id']}", format="metadata", metadataHeaders="Subject")
            messages = thread.get("messages", [])
            payload.update(
                thread_id=a["thread_id"],
                messages=len(messages),
                message=gmail.message_content(messages[0])["subject"] if messages else "",
            )
        else:
            payload["message_id"] = a["message_id"]
            payload["message"] = gmail.message_content(client.get(f"messages/{a['message_id']}", format="full"))[
                "subject"
            ]
    else:
        raise ValueError("operation must be create_draft, send_draft or labels")
    return service_actions.prepare(ctx["owner"], "gmail." + operation, payload)


SYSTEM_LABELS = {
    "starred": "STARRED",
    "星标": "STARRED",
    "unread": "UNREAD",
    "未读": "UNREAD",
    "inbox": "INBOX",
    "收件箱": "INBOX",
    "important": "IMPORTANT",
    "重要": "IMPORTANT",
}


def _label_ids(client):
    """Map of label ID -> display name for this mailbox."""
    return {label["id"]: label["name"] for label in client.get("labels").get("labels", [])}


def _resolve_label(value, names):
    """Accept a label ID, a label name (any case) or a common alias; unknown labels fail before review."""
    if value in names:
        return value
    if value.lower() in SYSTEM_LABELS:
        return SYSTEM_LABELS[value.lower()]
    matches = [label_id for label_id, name in names.items() if name.lower() == value.lower()]
    if len(matches) != 1:
        raise ValueError(f"Unknown Gmail label: {value}; use gmail_labels to list them")
    return matches[0]


def perform(kind, payload, scopes):
    # Every Rejected below is raised before contacting the upstream write endpoint.
    if kind.startswith("gmail."):
        if "gmail.manage" not in scopes:
            raise service_actions.Rejected("gmail.manage is required")
        client = gmail.Client()
        if client.get("profile")["emailAddress"].lower() != payload["account"].lower():
            raise service_actions.Rejected("Mailbox authorization changed; prepare again")
        if kind == "gmail.create_draft":
            return client.create_draft(gmail._decode(payload["raw"]), thread_id=payload.get("thread_id"))
        if kind == "gmail.send_draft":
            current = client.get(f"drafts/{payload['draft_id']}", format="raw")
            if hashlib.sha256(gmail._decode(current["message"]["raw"])).hexdigest() != payload["raw_sha256"]:
                raise service_actions.Rejected("The draft changed after review; prepare it again")
            return client.send_draft(payload["draft_id"])
        if kind == "gmail.labels":
            if payload.get("thread_id"):
                result = client.modify_thread_labels(payload["thread_id"], add=payload["add"], remove=payload["remove"])
                verified = client.get(f"threads/{payload['thread_id']}", format="minimal")
                return {
                    "response": {"id": result.get("id")},
                    "verified": [
                        {"id": m["id"], "labelIds": m.get("labelIds", [])} for m in verified.get("messages", [])
                    ],
                }
            result = client.modify_labels(payload["message_id"], add=payload["add"], remove=payload["remove"])
            return {"response": result, "verified": client.get(f"messages/{payload['message_id']}", format="minimal")}
    if kind == "attendance.submit":
        if "attendance.submit" not in scopes:
            raise service_actions.Rejected("attendance.submit is required")
        from . import attendance

        return attendance.submit(payload)
    if kind.startswith("ontrack."):
        if "ontrack.write" not in scopes:
            raise service_actions.Rejected("ontrack.write is required")
        from . import ontrack

        client = ontrack.OnTrackClient()
        if client.base_url != payload["base_url"] or client.session["username"] != payload["account"]:
            raise service_actions.Rejected("OnTrack account changed; prepare again")
        if kind == "ontrack.chat_read":
            return client.task_comments(payload["project_id"], payload["task_definition_id"])
        if kind == "ontrack.chat_send":
            return client.add_task_comment(payload["project_id"], payload["task_definition_id"], payload["message"])
        if kind == "ontrack.task_state":
            return client.update_task_state(payload["project_id"], payload["task_definition_id"], payload["state"])
    raise ValueError("Unsupported action")


def confirm(a, ctx):
    return service_actions.execute(
        ctx["owner"],
        a["action_id"],
        a["review_digest"],
        a.get("user_confirmed"),
        lambda kind, payload: perform(kind, payload, ctx["scopes"]),
    )


def attendance_call(operation, a, ctx):
    from . import attendance

    if operation == "status":
        return attendance.status()
    if operation == "sync":
        return attendance.sync(a.get("days", 7))
    if operation == "overview":
        return attendance.overview(a.get("days", 14))
    if operation == "codes":
        return attendance.find_codes(a.get("days", 7), gmail_allowed=allowed("gmail.read", ctx["scopes"]))
    if operation == "export":
        return attendance.export_history(a.get("days", 120))
    if operation == "prepare":
        payload = attendance.prepare(a["record_id"], a["code"], a.get("actually_attended"))
        return service_actions.prepare(ctx["owner"], "attendance.submit", payload)
    raise ValueError("Unknown attendance operation")


def ontrack_call(operation, a, ctx):
    from . import ontrack

    client = ontrack.OnTrackClient()
    if operation == "units":
        return client.projects(include_inactive=bool(a.get("include_inactive", False)))
    if operation == "tasks":
        if a.get("task"):
            return ontrack.task_detail(client, a["unit"], a["task"])
        raw_statuses = a.get("statuses") or ""
        statuses = [s.strip() for s in raw_statuses.split(",") if s.strip()] if raw_statuses else None
        return ontrack.list_tasks(client, a["unit"], statuses)
    if operation == "task_sheet":
        res = ontrack.read_task_sheet_markdown(client, a["unit"], a["task"])
        offset = max(0, int(a.get("offset", 0)))
        max_chars = min(max(1, int(a.get("max_chars", 20000))), 100000)
        text = res["markdown"]
        part = text[offset:offset + max_chars]
        if offset + max_chars < len(text):
            part += f"\n\n[还有 {len(text) - offset - max_chars} 字符未显示，用 offset={offset + max_chars} 继续读]"
        return {
            "project_id": res["project_id"],
            "unit_code": res["unit_code"],
            "task_definition_id": res["task_definition_id"],
            "task": res["task"],
            "name": res["name"],
            "filename": res["filename"],
            "pages": res["pages"],
            "markdown": part,
        }
    if operation == "chats":
        return ontrack.list_chats(client, a["unit"])
    if operation == "prepare":
        op = a["operation"]
        if op not in {"chat_read", "chat_send", "task_state"}:
            raise ValueError("operation must be chat_read, chat_send or task_state")
        snap = ontrack.project_snapshot(client, a["unit"])
        row, td = ontrack.select_task(snap, a["task"])
        payload = {
            "base_url": client.base_url,
            "account": client.session["username"],
            "project_id": snap["project"]["id"],
            "unit_code": snap["unit"]["code"],
            "task_definition_id": td["id"],
            "task": row["abbreviation"],
            "task_name": row["name"],
            "current_status": row["status"],
        }
        if op == "chat_read":
            payload["side_effect"] = "Fetching OnTrack task comments marks non-discussion comments as read."
        elif op == "chat_send":
            msg = (a.get("message") or "").strip()
            if not msg:
                raise ValueError("message is required for chat_send")
            if len(msg) > 4095:
                raise ValueError("message must not exceed 4095 characters")
            payload["message"] = msg
        elif op == "task_state":
            state = (a.get("state") or "").strip()
            if state not in ontrack.WRITABLE_TASK_STATES:
                raise ValueError(f"state must be one of: {', '.join(ontrack.WRITABLE_TASK_STATES)}")
            payload["state"] = state
        return service_actions.prepare(ctx["owner"], "ontrack." + op, payload)
    raise ValueError("Unknown ontrack operation")


S, I, B = "string", "integer", "boolean"
# handler, description, parameter schema, capability, upstream read-only
TOOLS = {
    "gmail_search": (
        mail_search,
        "用户的 Monash 学校邮箱（Gmail）。用户问“我的邮箱/邮件/有什么要处理的/某封学校邮件”时优先用这个，而不是其他通用 Gmail 连接器。用 Gmail 搜索语法实时查邮件并保存正文，例如 newer_than:14d -category:promotions；不标记已读。",
        {"query": (S, False), "limit": (I, False), "page_token": (S, False)},
        "gmail.read",
        True,
    ),
    "gmail_thread": (
        mail_thread,
        "读取 Monash 学校邮箱里的一整个对话并保存；refresh=false 只读缓存，offset/max_chars 分页。",
        {"thread_id": (S, True), "refresh": (B, False), "offset": (I, False), "max_chars": (I, False)},
        "gmail.read",
        True,
    ),
    "gmail_cache_search": (
        lambda a, c: with_mail(
            lambda cl, st: st.search(a.get("query", ""), limit=a.get("limit", 20), offset=a.get("offset", 0))
        ),
        "离线全文搜索已保存的 Monash 学校邮件正文（普通关键词，不是 Gmail 语法）；比 gmail_search 快，但只覆盖已同步的部分。",
        {"query": (S, False), "limit": (I, False), "offset": (I, False)},
        "gmail.read",
        True,
    ),
    "gmail_status": (
        lambda a, c: with_mail(lambda cl, st: st.status()),
        "Monash 学校邮箱镜像的账号、覆盖范围与同步状态。",
        {},
        "gmail.read",
        True,
    ),
    "gmail_sync": (
        lambda a, c: with_mail(
            lambda cl, st: gmail.sync(cl, st, query=a.get("query", "newer_than:90d"), page_limit=a.get("pages", 2))
        ),
        "下载邮件正文与增量变化；返回 more_pending 时继续调用。query 选择初始回填范围。",
        {"query": (S, False), "pages": (I, False)},
        "gmail.read",
        True,
    ),
    "gmail_attachment": (
        lambda a, c: with_mail(lambda cl, st: gmail.cache_attachment(cl, st, a["message_id"], a["part_id"])),
        "下载指定 MIME 附件到私有镜像，返回文件路径与哈希。",
        {"message_id": (S, True), "part_id": (S, True)},
        "gmail.read",
        True,
    ),
    "gmail_read_attachment": (
        lambda a, c: with_mail(
            lambda cl, st: gmail.read_attachment(
                cl, st, a["message_id"], a["part_id"], offset=a.get("offset", 0), max_chars=a.get("max_chars", 20000)
            )
        ),
        "读取邮件附件正文并加入全文索引；支持 PDF、DOCX、PPTX、文本与图片 OCR，支持字符分页。",
        {"message_id": (S, True), "part_id": (S, True), "offset": (I, False), "max_chars": (I, False)},
        "gmail.read",
        True,
    ),
    "gmail_labels": (lambda a, c: gmail.Client().get("labels"), "列邮箱标签及标签 ID。", {}, "gmail.read", True),
    "gmail_prepare_action": (
        mail_prepare,
        "准备邮箱操作并返回完整预览。operation=create_draft/send_draft/labels；labels 用 message_id（单封）或 thread_id（整个对话），add/remove 可写标签名或常用名（星标、未读、收件箱、重要），去掉“收件箱”即归档、去掉“未读”即标记已读；回复草稿可用 reply_to_message_id。必须向用户展示并获具体确认后才 execute_student_action。",
        {
            "operation": (S, True),
            "to": (S, False),
            "cc": (S, False),
            "bcc": (S, False),
            "subject": (S, False),
            "body": (S, False),
            "draft_id": (S, False),
            "message_id": (S, False),
            "reply_to_message_id": (S, False),
            "thread_id": (S, False),
            "add": (S, False),
            "remove": (S, False),
        },
        "gmail.manage",
        True,
    ),
    "attendance_status": (
        lambda a, c: attendance_call("status", a, c),
        "查看 Attendance 登录与同步健康状态。",
        {},
        "attendance.read",
        True,
    ),
    "attendance_sync": (
        lambda a, c: attendance_call("sync", a, c),
        "读取门户真实班次及完成状态并保存；days 最多14。",
        {"days": (I, False)},
        "attendance.read",
        True,
    ),
    "attendance_overview": (
        lambda a, c: attendance_call("overview", a, c),
        "读取已保存班次，区分已完成、未完成和未来班次。",
        {"days": (I, False)},
        "attendance.read",
        True,
    ),
    "attendance_find_codes": (
        lambda a, c: attendance_call("codes", a, c),
        "从已同步的 Moodle、Ed、Gmail 证据匹配签到码；保持缺失与冲突可见。",
        {"days": (I, False)},
        "attendance.read",
        True,
    ),
    "attendance_export": (
        lambda a, c: attendance_call("export", a, c),
        "导出已读取的历史签到记录为 CSV，说明覆盖范围。",
        {"days": (I, False)},
        "attendance.read",
        True,
    ),
    "attendance_prepare_submit": (
        lambda a, c: attendance_call("prepare", a, c),
        "仅当用户明确声明实际到课后准备签到；禁止给未来或已完成班次提交。",
        {"record_id": (S, True), "code": (S, True), "actually_attended": (B, True)},
        "attendance.submit",
        True,
    ),
    "ontrack_units": (
        lambda a, c: ontrack_call("units", a, c),
        "列出用户在 OnTrack / Doubtfire 的课程（项目）和项目 ID；include_inactive=true 包含往期课程。",
        {"include_inactive": (B, False)},
        "ontrack.read",
        True,
    ),
    "ontrack_tasks": (
        lambda a, c: ontrack_call("tasks", a, c),
        "查看 OnTrack 一门课的任务进度、截止日期、等级和未读评论数；传入 task 时返回单个任务详情与提交要求。",
        {"unit": (S, True), "task": (S, False), "statuses": (S, False)},
        "ontrack.read",
        True,
    ),
    "ontrack_task_sheet": (
        lambda a, c: ontrack_call("task_sheet", a, c),
        "读取 OnTrack 任务书 PDF 的 Markdown 正文（支持 offset/max_chars 分页）。",
        {"unit": (S, True), "task": (S, True), "offset": (I, False), "max_chars": (I, False)},
        "ontrack.read",
        True,
    ),
    "ontrack_chats": (
        lambda a, c: ontrack_call("chats", a, c),
        "只读查看 OnTrack 一门课各任务的未读导师评论数（不会把评论标为已读）。",
        {"unit": (S, True)},
        "ontrack.read",
        True,
    ),
    "ontrack_prepare_action": (
        lambda a, c: ontrack_call("prepare", a, c),
        "准备 OnTrack 写操作预览：operation=chat_read（读评论历史，OnTrack 会标已读）/chat_send（发评论）/task_state（改进度状态）。必须展示预览并获用户明确确认后再调 execute_student_action；不含提交作业。",
        {"operation": (S, True), "unit": (S, True), "task": (S, True), "message": (S, False), "state": (S, False)},
        "ontrack.write",
        True,
    ),
    "execute_student_action": (
        confirm,
        "执行已预览且用户明确确认的单次操作，user_confirmed 必须为 true；不得自行确认。",
        {"action_id": (S, True), "review_digest": (S, True), "user_confirmed": (B, True)},
        "write",
        False,
    ),
}


def allowed(scope, scopes):
    return (
        scope in scopes
        or (scope == "gmail.read" and "gmail.manage" in scopes)
        or (scope == "attendance.read" and "attendance.submit" in scopes)
        or (scope == "ontrack.read" and "ontrack.write" in scopes)
        or (scope == "write" and bool({"gmail.manage", "attendance.submit", "ontrack.write"} & scopes))
    )


def tool_list(scopes):
    out = []
    for name, (_, desc, params, scope, readonly) in TOOLS.items():
        if allowed(scope, scopes):
            if scope == "write":
                required_scope = next(
                    (s for s in ("gmail.manage", "attendance.submit", "ontrack.write") if s in scopes),
                    "gmail.manage",
                )
            else:
                required_scope = scope
            if required_scope not in scopes:
                if scope.startswith("gmail"):
                    required_scope = "gmail.manage"
                elif scope.startswith("ontrack"):
                    required_scope = "ontrack.write"
                else:
                    required_scope = "attendance.submit"
            out.append(
                {
                    "name": name,
                    "description": desc,
                    "inputSchema": {
                        "type": "object",
                        "properties": {k: {"type": v[0]} for k, v in params.items()},
                        "required": [k for k, v in params.items() if v[1]],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": readonly},
                    "securitySchemes": [
                        {
                            "type": "oauth2",
                            "scopes": (["moodle"] if "moodle" in scopes else ["monash"] if "monash" in scopes else [])
                            + [required_scope],
                        }
                    ],
                }
            )
    return out


def call(name, arguments, scopes, owner):
    spec = TOOLS[name]
    if not allowed(spec[3], scopes):
        raise PermissionError("This client has not authorized " + spec[3])
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    for key, (typ, required) in spec[2].items():
        if required and key not in arguments:
            raise ValueError("Missing argument: " + key)
        if key in arguments and (type(arguments[key]) is not {S: str, I: int, B: bool}[typ]):
            raise ValueError("Invalid argument type: " + key)
    if arguments.keys() - spec[2].keys():
        raise ValueError("Unknown arguments")
    context = {"scopes": scopes, "owner": owner}
    if name in {"gmail_sync", "attendance_sync", "attendance_find_codes", "attendance_export"}:
        permissions = {spec[3]}
        if name == "attendance_find_codes" and allowed("gmail.read", scopes):
            permissions.add("gmail.read")
        return service_jobs.submit(
            owner, name, arguments, lambda: spec[0](arguments, context), required_scopes=permissions
        )
    return spec[0](arguments, context)
