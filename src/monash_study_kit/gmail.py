"""Private Gmail mirror. Reading, local storage and remote writes are separate capabilities.

Google libraries are optional and loaded only for OAuth. Cache queries work offline.
No passwords or browser cookies are used, and no email content is logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .paths import DATA_DIR, SECRETS_DIR, ensure_private_dir, write_secret

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MANAGE_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
API = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN_FILE = SECRETS_DIR / "gmail-token.json"
DB_FILE = DATA_DIR / "gmail" / "gmail.db"


class GmailError(RuntimeError):
    pass


class GmailAuthError(GmailError):
    pass


class GmailHTTPError(GmailError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Gmail API returned HTTP {status}")


def _google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GmailError('Install Gmail support: pip install "monash-study-kit[gmail]"') from e
    return Request, Credentials, InstalledAppFlow


def login(client_file: Path, *, manage: bool = False, token_file: Path = TOKEN_FILE) -> dict:
    """Interactive installed-app OAuth with a loopback callback; no token enters chat."""
    _, _, Flow = _google()
    config = json.loads(client_file.read_text(encoding="utf-8"))
    if "installed" not in config:
        raise ValueError("Use a Google OAuth Desktop app client JSON file")
    scopes = [MANAGE_SCOPE if manage else READ_SCOPE]
    flow = Flow.from_client_config(config, scopes=scopes)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, access_type="offline",
        timeout_seconds=600, login_hint=os.environ.get("MONASH_KIT_GMAIL_ACCOUNT"),
        prompt="consent", authorization_prompt_message="Authorize Gmail in your browser.",
        success_message="Gmail connected. You can close this window.")
    if not credentials.refresh_token:
        raise GmailAuthError("Google did not issue an offline refresh token; reconnect Gmail")
    granted = credentials.granted_scopes or credentials.scopes or []
    if not set(scopes).issubset(granted):
        raise GmailAuthError("The requested Gmail permission was not granted")
    # Validate the mailbox before replacing a previously working token.
    profile = Client(credentials=credentials).get("profile")
    expected = os.environ.get("MONASH_KIT_GMAIL_ACCOUNT")
    if expected and profile["emailAddress"].lower()!=expected.lower():
        raise GmailAuthError("The selected Gmail account does not match the requested account; authorize the Monash mailbox")
    write_secret(token_file, credentials.to_json())
    return {"account": profile["emailAddress"], "mode": "manage" if manage else "sync"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to a redirected host.


class Client:
    def __init__(self, token_file: Path = TOKEN_FILE, *, credentials=None):
        self.token_file = token_file
        self.credentials = credentials

    def _credentials(self):
        if self.credentials is None:
            if not self.token_file.is_file():
                raise GmailAuthError(os.environ.get("MONASH_KIT_GMAIL_LOGIN_HINT","Run monash gmail login --client <desktop-client.json>"))
            _, Credentials, _ = _google()
            try:
                self.credentials = Credentials.from_authorized_user_file(str(self.token_file))
            except (ValueError, OSError) as e:
                raise GmailAuthError("Gmail credentials cannot be loaded; reconnect Gmail") from e
        if not self.credentials.valid:
            Request, _, _ = _google()
            try:
                self.credentials.refresh(Request())
            except Exception as e:
                raise GmailAuthError("Gmail authorization expired or refresh failed; check connectivity and reconnect if needed") from e
            write_secret(self.token_file, self.credentials.to_json())
        return self.credentials

    def request(self, method: str, endpoint: str, *, params=None, body=None):
        if not re.fullmatch(r"[A-Za-z0-9_/-]+", endpoint) or ".." in endpoint:
            raise ValueError("Invalid Gmail endpoint")
        credentials = self._credentials()
        if method != "GET" and MANAGE_SCOPE not in (credentials.scopes or []):
            raise GmailAuthError("Mailbox changes require monash gmail login --manage")
        url = API + endpoint
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + credentials.token, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(_NoRedirect())
        for attempt in range(3):
            try:
                with opener.open(req, timeout=45) as response:
                    raw = response.read(64 * 1024 * 1024 + 1)
                    if len(raw) > 64 * 1024 * 1024:
                        raise GmailError("Gmail response exceeded the size limit")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                status = e.code
                e.close()
                # Never automatically retry a write: the server may have applied it.
                if method == "GET" and status in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                if status == 401:
                    raise GmailAuthError("Gmail rejected authorization; reconnect Gmail") from None
                raise GmailHTTPError(status) from None

    def get(self, endpoint: str, **params):
        return self.request("GET", endpoint, params=params)

    def modify_labels(self, message_id: str, *, add=(), remove=()):
        return self.request("POST", f"messages/{message_id}/modify",
                            body={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def create_draft(self, raw: bytes, *, thread_id=None):
        message = {"raw":base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            message["threadId"]=thread_id
        return self.request("POST", "drafts", body={"message":message})

    def send_draft(self, draft_id: str):
        return self.request("POST", "drafts/send", body={"id": draft_id})


def live_search(client, store, query, *, limit=20, page_token=None):
    profile = client.get("profile")
    account = profile["emailAddress"].lower()
    if store.state("account") not in (None, account):
        raise GmailError("This cache belongs to another mailbox")
    page = client.get("messages", q=query, maxResults=max(1, min(int(limit), 100)), pageToken=page_token)
    try:
        store.set_state("account", account)
        ids = []
        for item in page.get("messages", []):
            message = client.get(f"messages/{item['id']}", format="full")
            _materialize_bodies(client, message)
            store.put(message)
            ids.append(message["id"])
        store.con.commit()
    except Exception:
        store.con.rollback()
        raise
    rows = []
    for mid in ids:
        row = store.con.execute("SELECT id,thread_id,subject,sender,body FROM messages WHERE id=?", (mid,)).fetchone()
        rows.append({k: (v[:400] if k == "body" else v) for k, v in dict(row).items()})
    return {"items": rows, "next_page_token": page.get("nextPageToken"), "query": query, "live": True}


def live_thread(client, store, thread_id):
    profile = client.get("profile")
    account = profile["emailAddress"].lower()
    if store.state("account") not in (None, account):
        raise GmailError("This cache belongs to another mailbox")
    thread = client.get(f"threads/{thread_id}", format="full")
    try:
        store.set_state("account", account)
        remote_ids = {message["id"] for message in thread.get("messages",[])}
        cached_ids = [row[0] for row in store.con.execute("SELECT id FROM messages WHERE thread_id=? AND deleted=0",(thread_id,))]
        for message_id in cached_ids:
            if message_id not in remote_ids:
                store.tombstone(message_id)
        for message in thread.get("messages", []):
            _materialize_bodies(client, message)
            store.put(message)
        store.con.commit()
    except Exception:
        store.con.rollback()
        raise
    return {"thread_id": thread_id, "downloaded_messages": len(thread.get("messages", [])), "live": True}


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def message_content(message: dict) -> dict:
    """Extract bodies and attachment metadata; retain the full payload separately."""
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h.get("value", "") for h in payload.get("headers", [])}
    plain, html, attachments = [], [], []

    def walk(part):
        body = part.get("body", {})
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        if filename or body.get("attachmentId"):
            attachments.append({"part_id": part.get("partId", ""), "filename": filename,
                                "mime_type": mime, "size": body.get("size", 0),
                                "attachment_id": body.get("attachmentId"),
                                "inline_data": body.get("data")})
        elif body.get("data") and mime in {"text/plain", "text/html"}:
            part_headers = {h["name"].lower(): h.get("value", "") for h in part.get("headers", [])}
            charset = re.search(r'charset=["\s]*([^;"\s]+)', part_headers.get("content-type", ""), re.I)
            try:
                value = _decode(body["data"]).decode(charset[1] if charset else "utf-8", errors="replace")
            except LookupError:
                value = _decode(body["data"]).decode("utf-8", errors="replace")
            (plain if mime == "text/plain" else html).append(value)
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    else:
        parser = _HTMLText()
        parser.feed("\n".join(html))
        text = "".join(parser.parts)
    return {"subject": headers.get("subject", ""), "sender": headers.get("from", ""),
            "recipients": headers.get("to", ""), "date": headers.get("date", ""),
            "body": text, "attachments": attachments}


class Store:
    def __init__(self, db_file: Path = DB_FILE):
        self.path = Path(db_file)
        ensure_private_dir(self.path.parent)
        # Set permissions before SQLite writes any private content.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.con = sqlite3.connect(self.path, timeout=5)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, internal_date INTEGER NOT NULL,
              subject TEXT, sender TEXT, recipients TEXT, body TEXT, labels TEXT,
              payload TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, synced_at INTEGER);
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(id UNINDEXED, subject, sender, body);
            CREATE TABLE IF NOT EXISTS attachments (
              message_id TEXT NOT NULL, part_id TEXT NOT NULL, sha256 TEXT NOT NULL,
              size INTEGER NOT NULL, filename TEXT, PRIMARY KEY(message_id, part_id));
            CREATE TABLE IF NOT EXISTS attachment_text (
              message_id TEXT NOT NULL, part_id TEXT NOT NULL, text TEXT NOT NULL,
              PRIMARY KEY(message_id,part_id));
            CREATE TABLE IF NOT EXISTS attachment_meta (
              message_id TEXT NOT NULL,part_id TEXT NOT NULL,filename TEXT,mime_type TEXT,size INTEGER,
              PRIMARY KEY(message_id,part_id));
            CREATE TABLE IF NOT EXISTS attachment_failures (
              message_id TEXT NOT NULL,part_id TEXT NOT NULL,attempts INTEGER NOT NULL,error TEXT,
              PRIMARY KEY(message_id,part_id));
        """)

    def close(self):
        self.con.close()

    def state(self, key, default=None):
        row = self.con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))

    def put(self, message):
        content = message_content(message)
        self.con.execute("DELETE FROM attachment_meta WHERE message_id=?",(message["id"],))
        for attachment in content["attachments"]:
            self.con.execute("INSERT OR REPLACE INTO attachment_meta VALUES (?,?,?,?,?)",(
                message["id"],attachment["part_id"],attachment["filename"],attachment["mime_type"],attachment["size"]))
        self.con.execute("""INSERT OR REPLACE INTO messages
            (id,thread_id,internal_date,subject,sender,recipients,body,labels,payload,deleted,synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,0,?)""", (
                message["id"], message.get("threadId", message["id"]), int(message.get("internalDate", 0)),
                content["subject"], content["sender"], content["recipients"], content["body"],
                json.dumps(message.get("labelIds", [])), json.dumps(message), int(time.time())))
        self.con.execute("DELETE FROM message_fts WHERE id=?", (message["id"],))
        attachments = "\n".join(row[0] for row in self.con.execute("SELECT text FROM attachment_text WHERE message_id=?",(message["id"],)))
        self.con.execute("INSERT INTO message_fts VALUES (?,?,?,?)", (
            message["id"], content["subject"], content["sender"], content["body"]+"\n"+attachments))

    def tombstone(self, message_id):
        self.con.execute("UPDATE messages SET deleted=1 WHERE id=?", (message_id,))
        self.con.execute("DELETE FROM message_fts WHERE id=?", (message_id,))

    def status(self):
        return {"account": self.state("account"), "query": self.state("query"), "configured":TOKEN_FILE.is_file(),
                "last_success": self.state("last_success"), "history_id": self.state("history_id"),
                "initial_sync_pending": self.state("initial_pending") == "1",
                "cached_messages": self.con.execute("SELECT count(*) FROM messages WHERE deleted=0").fetchone()[0],
                "attachments": "Metadata, downloaded bytes and indexed text are counted separately",
                "deletion_reconciliation_pending": self.state("needs_reconcile")=="1",
                "indexed_attachments": self.con.execute("SELECT count(*) FROM attachment_text").fetchone()[0],
                "attachment_metadata": self.con.execute("SELECT count(*) FROM attachment_meta").fetchone()[0],
                "downloaded_attachments": self.con.execute("SELECT count(*) FROM attachments").fetchone()[0],
                "cache_path": str(self.path)}

    def search(self, query: str = "", *, limit=20, offset=0):
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        params = []
        if query.strip():
            terms = re.findall(r"\S+", query)
            # Literal terms: user text never becomes FTS syntax or SQL.
            match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
            sql = "SELECT m.* FROM messages m JOIN message_fts f ON f.id=m.id WHERE message_fts MATCH ? AND m.deleted=0"
            params.append(match)
        else:
            sql = "SELECT * FROM messages WHERE deleted=0"
        rows = self.con.execute(sql + " ORDER BY internal_date DESC LIMIT ? OFFSET ?", (*params, limit + 1, offset)).fetchall()
        return {"items": [{"id": r["id"], "thread_id": r["thread_id"], "subject": r["subject"],
                           "sender": r["sender"], "snippet": r["body"][:350], "internal_date": r["internal_date"],
                           "source": f"https://mail.google.com/mail/#all/{r['thread_id']}"} for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None, "coverage": self.status()}

    def read_thread(self, thread_id: str, *, offset=0, max_chars=20000):
        rows = self.con.execute("SELECT * FROM messages WHERE thread_id=? AND deleted=0 ORDER BY internal_date", (thread_id,)).fetchall()
        if not rows:
            raise LookupError("Thread is not cached; sync Gmail or use live search first")
        text = "\n\n".join(f"From: {r['sender']}\nTo: {r['recipients']}\nSubject: {r['subject']}\n\n{r['body']}" for r in rows)
        offset, max_chars = max(0, int(offset)), max(1, min(50000, int(max_chars)))
        return {"thread_id": thread_id, "text": text[offset:offset + max_chars],
                "next_offset": offset + max_chars if len(text) > offset + max_chars else None,
                "messages": [{"id": r["id"], "attachments": [
                    {k: v for k, v in a.items() if k != "inline_data"}
                    for a in message_content(json.loads(r["payload"]))["attachments"]]} for r in rows],
                "coverage": "Cached messages only; a thread may have older or newer messages outside sync coverage"}


def _materialize_bodies(client: Client, message: dict):
    """Gmail can move even a text body into the attachment API when it is large."""
    def visit(part):
        body = part.get("body", {})
        if not part.get("filename") and part.get("mimeType") in {"text/plain", "text/html"} and body.get("attachmentId"):
            data = client.get(f"messages/{message['id']}/attachments/{body['attachmentId']}")
            part["body"] = {**body, "data": data["data"]}
            part["body"].pop("attachmentId", None)
        for child in part.get("parts", []):
            visit(child)
    visit(message.get("payload", {}))


def sync(client: Client, store: Store, *, query="newer_than:90d", page_limit=5) -> dict:
    """Bounded, resumable sync. Persist the cursor only with the corresponding data."""
    if not 1 <= page_limit <= 100:
        raise ValueError("page_limit must be between 1 and 100")
    con = store.con
    con.execute("BEGIN IMMEDIATE")
    try:
        profile = client.get("profile")
        account = profile["emailAddress"].lower()
        if store.state("account") not in (None, account):
            raise GmailError("This cache belongs to another mailbox; choose a separate MONASH_KIT_HOME")
        store.set_state("account", account)
        initial = store.state("query") != query or not store.state("history_id") or store.state("initial_pending") == "1"
        if initial and (store.state("query") != query or store.state("initial_pending") != "1"):
            store.set_state("query", query)
            store.set_state("initial_pending", "1")
            store.set_state("page_token", "")
            store.set_state("baseline", profile["historyId"])
        changed = 0
        for _ in range(page_limit):
            try:
                if initial:
                    page = client.get("messages", q=query, maxResults=100, pageToken=store.state("page_token") or None)
                    ids = {m["id"] for m in page.get("messages", [])}
                else:
                    page = client.get("history", startHistoryId=store.state("history_id"), maxResults=100,
                                      pageToken=store.state("page_token") or None)
                    ids = set()
                    for record in page.get("history", []):
                        for event in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
                            ids.update(item["message"]["id"] for item in record.get(event, []))
            except GmailHTTPError as e:
                if e.status == 404 and not initial:
                    store.set_state("history_id", "")
                    store.set_state("page_token", "")
                    store.set_state("needs_reconcile", "1")
                    store.set_state("reconcile_cursor", "")
                    con.commit()
                    return {"status": "resync_required", "reason": "Gmail history expired; run sync again", **store.status()}
                raise
            for message_id in sorted(ids):
                try:
                    message = client.get(f"messages/{message_id}", format="full")
                except GmailHTTPError as e:
                    if e.status != 404:
                        raise
                    store.tombstone(message_id)
                else:
                    _materialize_bodies(client, message)
                    store.put(message)
                changed += 1
            next_page = page.get("nextPageToken", "")
            store.set_state("page_token", next_page)
            if not next_page:
                store.set_state("history_id", store.state("baseline") if initial else page["historyId"])
                store.set_state("initial_pending", "0")
                store.set_state("last_success", int(time.time()))
                break
        if not next_page and store.state("needs_reconcile")=="1":
            reconcile_cache(client,store)
        con.commit()
        return {"status": "more_pending" if next_page else "ok", "processed": changed, **store.status()}
    except Exception:
        con.rollback()
        raise


def reconcile_cache(client,store,limit=50):
    """After an expired history cursor, verify old cached IDs in bounded batches."""
    rows = store.con.execute("SELECT id,payload FROM messages WHERE deleted=0 AND id>? ORDER BY id LIMIT ?",
                             (store.state("reconcile_cursor",""),limit)).fetchall()
    for message_id,payload in rows:
        try:
            current = client.get(f"messages/{message_id}",format="minimal")
        except GmailHTTPError as error:
            if error.status!=404:
                raise
            store.tombstone(message_id)
        else:
            cached = json.loads(payload)
            cached["labelIds"] = current.get("labelIds",cached.get("labelIds",[]))
            store.put(cached)
        store.set_state("reconcile_cursor",message_id)
    if len(rows)<limit:
        store.set_state("needs_reconcile","0")


def cache_attachment(client: Client, store: Store, message_id: str, part_id: str, *, max_bytes=25 * 1024 * 1024):
    row = store.con.execute("SELECT payload FROM messages WHERE id=? AND deleted=0", (message_id,)).fetchone()
    if not row:
        raise LookupError("Message is not cached")
    attachments = message_content(json.loads(row[0]))["attachments"]
    attachment = next((a for a in attachments if a["part_id"] == part_id), None)
    if attachment is None:
        raise LookupError("Attachment part not found")
    if attachment["size"] > max_bytes:
        raise GmailError("Attachment exceeds the configured size limit")
    cached = store.con.execute("SELECT * FROM attachments WHERE message_id=? AND part_id=?", (message_id, part_id)).fetchone()
    if cached:
        target = store.path.parent / "attachments" / cached["sha256"]
        if target.is_file():
            if cached["size"] > max_bytes:
                raise GmailError("Attachment exceeds the configured size limit")
            return {"filename": cached["filename"], "path": str(target), "size": cached["size"],
                    "sha256": cached["sha256"], "cached": True}
    # OAuth identity must match the cache even if the attachment is downloaded later.
    if client.get("profile")["emailAddress"].lower() != store.state("account"):
        raise GmailError("Gmail authorization belongs to another mailbox")
    data = attachment.get("inline_data")
    if data is None:
        data = client.get(f"messages/{message_id}/attachments/{attachment['attachment_id']}")["data"]
    raw = _decode(data)
    if len(raw) > max_bytes:
        raise GmailError("Attachment exceeds the configured size limit")
    digest = hashlib.sha256(raw).hexdigest()
    directory = store.path.parent / "attachments"
    ensure_private_dir(directory)
    target = directory / digest  # Remote filenames can never escape the private directory.
    temporary = directory / f".{digest}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    store.con.execute("INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?)", (
        message_id, part_id, digest, len(raw), attachment["filename"]))
    store.con.commit()
    return {"filename": attachment["filename"], "path": str(target), "size": len(raw), "sha256": digest, "cached": False}


def read_attachment(client,store,message_id,part_id,*,offset=0,max_chars=20000):
    from .attendance_evidence import extract_file
    saved = cache_attachment(client,store,message_id,part_id)
    row = store.con.execute("SELECT text FROM attachment_text WHERE message_id=? AND part_id=?",(message_id,part_id)).fetchone()
    text = row[0] if row else extract_file(saved["path"],saved["filename"])
    if not row:
        store.con.execute("INSERT OR REPLACE INTO attachment_text VALUES (?,?,?)",(message_id,part_id,text))
        payload = store.con.execute("SELECT payload FROM messages WHERE id=?",(message_id,)).fetchone()
        store.put(json.loads(payload[0]))
        store.con.commit()
    offset,max_chars=max(0,int(offset)),max(1,min(int(max_chars),50000))
    return {**saved,"text":text[offset:offset+max_chars],"next_offset":offset+max_chars if len(text)>offset+max_chars else None,
            "extraction":"available" if text else "No text extracted for this file type; use the original attachment"}


def sync_attachments(client,store,limit=5):
    rows = store.con.execute("""SELECT a.message_id,a.part_id FROM attachment_meta a
        JOIN messages m ON m.id=a.message_id
        LEFT JOIN attachment_text t ON t.message_id=a.message_id AND t.part_id=a.part_id
        LEFT JOIN attachment_failures f ON f.message_id=a.message_id AND f.part_id=a.part_id
        WHERE m.deleted=0 AND t.message_id IS NULL AND COALESCE(f.attempts,0)<3
        ORDER BY m.internal_date DESC LIMIT ?""",(max(1,min(int(limit),20)),)).fetchall()
    processed,errors=0,[]
    for mid,part in rows:
        try:
            read_attachment(client,store,mid,part,max_chars=1)
            processed+=1
        except Exception as e:
            store.con.execute("INSERT INTO attachment_failures VALUES (?,?,1,?) "
                "ON CONFLICT(message_id,part_id) DO UPDATE SET attempts=attempts+1,error=excluded.error",(mid,part,type(e).__name__))
            store.con.commit()
            errors.append({"message_id":mid,"part_id":part,"error":type(e).__name__})
    return {"processed":processed,"errors":errors,"coverage":store.status()}
