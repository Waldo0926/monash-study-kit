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
    return with_mail(lambda client, store: gmail.live_search(client, store, a.get("query", ""),
                    limit=a.get("limit", 20), page_token=a.get("page_token")))


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
            parent = client.get(f"messages/{a['reply_to_message_id']}",format="full")
            headers = {h["name"].lower():h["value"] for h in parent.get("payload",{}).get("headers",[])}
            original_id = headers.get("message-id")
            if not original_id:
                raise ValueError("The original message has no Message-ID; reply in Gmail instead")
            message["In-Reply-To"]=original_id
            message["References"]=(headers.get("references","")+" "+original_id).strip()
            thread_id = parent["threadId"]
        for header, key in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"), ("Subject", "subject")):
            if a.get(key):
                message[header] = a[key]  # EmailMessage rejects header injection.
        if not a.get("to"):
            raise ValueError("A recipient is required")
        message.set_content(a.get("body", ""))
        payload = {"account": account, "to": a["to"], "cc": a.get("cc", ""), "bcc": a.get("bcc", ""),
                   "subject": a.get("subject", ""), "body": a.get("body", ""),
                   "thread_id":thread_id,"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    elif operation == "send_draft":
        draft = client.get(f"drafts/{a['draft_id']}", format="raw")
        raw = draft["message"]["raw"]
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(gmail._decode(raw))
        body = (message.get_body(preferencelist=("plain", "html")) or message).get_content()
        payload = {"account": account, "draft_id": a["draft_id"], "raw_sha256": hashlib.sha256(gmail._decode(raw)).hexdigest(),
                   "to": message.get("To", ""), "cc": message.get("Cc", ""), "bcc": message.get("Bcc", ""),
                   "subject": message.get("Subject", ""), "body": body,
                   "attachments": [p.get_filename() for p in message.iter_attachments()]}
    elif operation == "labels":
        payload = {"account": account, "message_id": a["message_id"],
                   "add": [v.strip() for v in a.get("add", "").split(",") if v.strip()],
                   "remove": [v.strip() for v in a.get("remove", "").split(",") if v.strip()]}
        payload["message"] = gmail.message_content(client.get(f"messages/{a['message_id']}", format="full"))["subject"]
    else:
        raise ValueError("operation must be create_draft, send_draft or labels")
    return service_actions.prepare(ctx["owner"], "gmail." + operation, payload)


def perform(kind, payload, scopes):
    if kind.startswith("gmail."):
        if "gmail.manage" not in scopes:
            raise PermissionError("gmail.manage is required")
        client = gmail.Client()
        if client.get("profile")["emailAddress"].lower() != payload["account"].lower():
            raise ValueError("Mailbox authorization changed; prepare again")
        if kind == "gmail.create_draft":
            return client.create_draft(gmail._decode(payload["raw"]),thread_id=payload.get("thread_id"))
        if kind == "gmail.send_draft":
            current = client.get(f"drafts/{payload['draft_id']}", format="raw")
            if hashlib.sha256(gmail._decode(current["message"]["raw"])).hexdigest() != payload["raw_sha256"]:
                raise ValueError("The draft changed after review; prepare it again")
            return client.send_draft(payload["draft_id"])
        if kind == "gmail.labels":
            result = client.modify_labels(payload["message_id"], add=payload["add"], remove=payload["remove"])
            return {"response": result, "verified": client.get(f"messages/{payload['message_id']}", format="minimal")}
    if kind == "attendance.submit":
        if "attendance.submit" not in scopes:
            raise PermissionError("attendance.submit is required")
        from . import attendance
        return attendance.submit(payload)
    raise ValueError("Unsupported action")


def confirm(a, ctx):
    return service_actions.execute(ctx["owner"], a["action_id"], a["review_digest"], a.get("user_confirmed"),
                                   lambda kind, payload: perform(kind, payload, ctx["scopes"]))


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


S, I, B = "string", "integer", "boolean"
# handler, description, parameter schema, capability, upstream read-only
TOOLS = {
    "gmail_search": (mail_search, "用 Gmail 搜索语法实时查邮件并保存正文；不标记已读。", {"query": (S, False), "limit": (I, False), "page_token": (S, False)}, "gmail.read", True),
    "gmail_thread": (mail_thread, "读取完整 Gmail 对话并保存；refresh=false 只读缓存，offset/max_chars 分页。", {"thread_id": (S, True), "refresh": (B, False), "offset": (I, False), "max_chars": (I, False)}, "gmail.read", True),
    "gmail_cache_search": (lambda a,c: with_mail(lambda cl,st: st.search(a.get("query", ""), limit=a.get("limit",20),offset=a.get("offset",0))), "离线全文搜索已保存邮件正文。", {"query": (S, False), "limit": (I, False), "offset": (I, False)}, "gmail.read", True),
    "gmail_status": (lambda a,c: with_mail(lambda cl,st: st.status()), "邮件镜像覆盖范围与同步状态。", {}, "gmail.read", True),
    "gmail_sync": (lambda a,c: with_mail(lambda cl,st: gmail.sync(cl,st,query=a.get("query","newer_than:90d"),page_limit=a.get("pages",2))), "下载邮件正文与增量变化；返回 more_pending 时继续调用。query 选择初始回填范围。", {"query": (S, False), "pages": (I, False)}, "gmail.read", True),
    "gmail_attachment": (lambda a,c: with_mail(lambda cl,st: gmail.cache_attachment(cl,st,a["message_id"],a["part_id"])), "下载指定 MIME 附件到私有镜像，返回文件路径与哈希。", {"message_id": (S, True), "part_id": (S, True)}, "gmail.read", True),
    "gmail_read_attachment": (lambda a,c: with_mail(lambda cl,st: gmail.read_attachment(cl,st,a["message_id"],a["part_id"],offset=a.get("offset",0),max_chars=a.get("max_chars",20000))), "读取邮件附件正文并加入全文索引；支持 PDF、DOCX、PPTX、文本与图片 OCR，支持字符分页。", {"message_id": (S, True), "part_id": (S, True), "offset": (I, False), "max_chars": (I, False)}, "gmail.read", True),
    "gmail_labels": (lambda a,c: gmail.Client().get("labels"), "列邮箱标签及标签 ID。", {}, "gmail.read", True),
    "gmail_prepare_action": (mail_prepare, "准备邮箱操作并返回完整预览。operation=create_draft/send_draft/labels；回复草稿可用 reply_to_message_id。必须向用户展示并获具体确认后才 execute_student_action。", {"operation": (S, True), "to": (S, False), "cc": (S, False), "bcc": (S, False), "subject": (S, False), "body": (S, False), "draft_id": (S, False), "message_id": (S, False), "reply_to_message_id": (S, False), "add": (S, False), "remove": (S, False)}, "gmail.manage", True),
    "attendance_status": (lambda a,c: attendance_call("status",a,c), "查看 Attendance 登录与同步健康状态。", {}, "attendance.read", True),
    "attendance_sync": (lambda a,c: attendance_call("sync",a,c), "读取门户真实班次及完成状态并保存；days 最多14。", {"days": (I, False)}, "attendance.read", True),
    "attendance_overview": (lambda a,c: attendance_call("overview",a,c), "读取已保存班次，区分已完成、未完成和未来班次。", {"days": (I, False)}, "attendance.read", True),
    "attendance_find_codes": (lambda a,c: attendance_call("codes",a,c), "从已同步的 Moodle、Ed、Gmail 证据匹配签到码；保持缺失与冲突可见。", {"days": (I, False)}, "attendance.read", True),
    "attendance_export": (lambda a,c: attendance_call("export",a,c), "导出已读取的历史签到记录为 CSV，说明覆盖范围。", {"days": (I, False)}, "attendance.read", True),
    "attendance_prepare_submit": (lambda a,c: attendance_call("prepare",a,c), "仅当用户明确声明实际到课后准备签到；禁止给未来或已完成班次提交。", {"record_id": (S, True), "code": (S, True), "actually_attended": (B, True)}, "attendance.submit", True),
    "execute_student_action": (confirm, "执行已预览且用户明确确认的单次操作，user_confirmed 必须为 true；不得自行确认。", {"action_id": (S, True), "review_digest": (S, True), "user_confirmed": (B, True)}, "write", False),
}


def allowed(scope, scopes):
    return (scope in scopes or (scope == "gmail.read" and "gmail.manage" in scopes)
            or (scope == "attendance.read" and "attendance.submit" in scopes)
            or (scope == "write" and bool({"gmail.manage", "attendance.submit"} & scopes)))


def tool_list(scopes):
    out = []
    for name, (_, desc, params, scope, readonly) in TOOLS.items():
        if allowed(scope, scopes):
            required_scope = ("gmail.manage" if "gmail.manage" in scopes else "attendance.submit") if scope == "write" else scope
            if required_scope not in scopes:
                required_scope = "gmail.manage" if scope.startswith("gmail") else "attendance.submit"
            out.append({"name": name, "description": desc,
                        "inputSchema": {"type": "object", "properties": {k: {"type": v[0]} for k,v in params.items()},
                                        "required": [k for k,v in params.items() if v[1]], "additionalProperties": False},
                        "annotations": {"readOnlyHint": readonly},
                        "securitySchemes": [{"type": "oauth2", "scopes": (["moodle"] if "moodle" in scopes else ["monash"] if "monash" in scopes else [])+[required_scope]}]})
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
        if key in arguments and (type(arguments[key]) is not {S:str,I:int,B:bool}[typ]):
            raise ValueError("Invalid argument type: " + key)
    if arguments.keys() - spec[2].keys():
        raise ValueError("Unknown arguments")
    context = {"scopes":scopes,"owner":owner}
    if name in {"gmail_sync","attendance_sync","attendance_find_codes","attendance_export"}:
        permissions={spec[3]}
        if name=="attendance_find_codes" and allowed("gmail.read",scopes):
            permissions.add("gmail.read")
        return service_jobs.submit(owner,name,arguments,lambda:spec[0](arguments,context),required_scopes=permissions)
    return spec[0](arguments, context)
