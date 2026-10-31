import base64

import pytest

from monash_study_kit import gmail
from monash_study_kit import student_services as S


def test_live_search_and_complete_thread_save_messages(tmp_path):
    store = gmail.Store(tmp_path/"gmail.db")
    def message(mid):
        return {"id":mid,"threadId":"thread","payload":{"mimeType":"text/plain","body":{"data":base64.urlsafe_b64encode(b"saved body").decode()}}}
    class Client:
        def get(self,path,**params):
            return {"profile":{"emailAddress":"user@example.edu"},"messages":{"messages":[{"id":"a"}]},
                    "messages/a":message("a"),"threads/thread":{"messages":[message("a"),message("b")]}}[path]
    result = gmail.live_search(Client(),store,"exchange")
    assert result["items"][0]["id"]=="a"
    assert gmail.live_thread(Client(),store,"thread")["downloaded_messages"]==2
    assert len(store.read_thread("thread")["messages"])==2
    store.close()


def test_reviewed_draft_change_blocks_send(monkeypatch):
    calls = []
    class Client:
        def get(self,path,**params):
            if path=="profile":
                return {"emailAddress":"user@example.edu"}
            return {"message":{"raw":base64.urlsafe_b64encode(b"changed content").decode()}}
        def send_draft(self,draft_id):
            calls.append(draft_id)
    monkeypatch.setattr(gmail,"Client",Client)
    with pytest.raises(ValueError,match="changed after review"):
        S.perform("gmail.send_draft",{"account":"user@example.edu","draft_id":"d","raw_sha256":"old"},{"gmail.manage"})
    assert calls==[]


def test_mail_preparation_rejects_header_injection(monkeypatch):
    class Client:
        def get(self,*a,**kw):
            return {"emailAddress":"user@example.edu"}
    monkeypatch.setattr(gmail,"Client",Client)
    with pytest.raises(ValueError):
        S.mail_prepare({"operation":"create_draft","to":"user@example.edu\nBcc: attacker@example.edu"},{"owner":"client"})


def test_downloaded_attachment_is_offline_readable_and_searchable(tmp_path):
    store = gmail.Store(tmp_path/"gmail.db")
    store.set_state("account","user@example.edu")
    store.put({"id":"a","threadId":"t","payload":{"mimeType":"text/plain","filename":"requirements.txt","partId":"1",
        "body":{"size":12,"data":base64.urlsafe_b64encode(b"exchange mapping evidence").decode()}}})
    store.con.commit()
    class Client:
        def get(self,*a,**kw):
            return {"emailAddress":"user@example.edu"}
    result = gmail.read_attachment(Client(),store,"a","1")
    assert "mapping evidence" in result["text"]
    assert store.search("mapping")["items"][0]["id"]=="a"
    class Offline:
        def get(self,*a,**kw):
            pytest.fail("Reading a saved indexed attachment must work offline")
    assert gmail.read_attachment(Offline(),store,"a","1")["cached"]
    store.close()


def test_reconciliation_marks_missing_remote_messages_and_preserves_files(tmp_path):
    store = gmail.Store(tmp_path/"gmail.db")
    for mid in ("a","b"):
        store.put({"id":mid,"threadId":"t","payload":{}})
    store.set_state("needs_reconcile","1");store.con.commit()
    class Client:
        def get(self,endpoint,**kw):
            if endpoint=="messages/a":
                raise gmail.GmailHTTPError(404)
            return {"labelIds":["INBOX"]}
    gmail.reconcile_cache(Client(),store)
    store.con.commit()
    assert store.state("needs_reconcile")=="0"
    assert [r["id"] for r in store.search()["items"]]==["b"]
    store.close()


def test_setup_rejects_accidental_authorization_of_another_mailbox(tmp_path,monkeypatch):
    client_file=tmp_path/"client.json"
    client_file.write_text('{"installed":{}}')
    token_file=tmp_path/"token.json"
    captured={}
    class Credentials:
        refresh_token="test-only"
        granted_scopes=[gmail.MANAGE_SCOPE]
        scopes=granted_scopes
    class Flow:
        @classmethod
        def from_client_config(cls,*a,**kw):
            return cls()
        def run_local_server(self,**kw):
            captured.update(kw)
            return Credentials()
    class Client:
        def __init__(self,**kw):pass
        def get(self,*a,**kw):return {"emailAddress":"wrong@example.edu"}
    monkeypatch.setattr(gmail,"_google",lambda:(None,None,Flow))
    monkeypatch.setattr(gmail,"Client",Client)
    monkeypatch.setenv("MONASH_KIT_GMAIL_ACCOUNT","correct@example.edu")
    with pytest.raises(gmail.GmailAuthError,match="does not match"):
        gmail.login(client_file,manage=True,token_file=token_file)
    assert captured["login_hint"]=="correct@example.edu" and not token_file.exists()


def _rate_limited_opener(calls, failures):
    import io, json, urllib.error

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, req, **kw):
            calls.append(req.get_method())
            if len(calls) <= failures:
                body = json.dumps({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}).encode()
                raise urllib.error.HTTPError(req.full_url, 403, "quota", {}, io.BytesIO(body))
            return Response(b'{"emailAddress": "me@example.edu"}')
    return Opener()


class _Credentials:
    valid = True
    scopes = [gmail.MANAGE_SCOPE]
    token = "do-not-log"


def test_rate_limited_read_waits_and_retries(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(gmail.urllib.request, "build_opener", lambda *a: _rate_limited_opener(calls, 2))
    monkeypatch.setattr(gmail.time, "sleep", sleeps.append)
    assert gmail.Client(credentials=_Credentials()).get("profile")["emailAddress"] == "me@example.edu"
    assert len(calls) == 3 and [s for s in sleeps if s >= 15] == [15, 30]


def test_rate_limited_write_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail.urllib.request, "build_opener", lambda *a: _rate_limited_opener(calls, 5))
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)
    with pytest.raises(gmail.GmailHTTPError) as raised:
        gmail.Client(credentials=_Credentials()).send_draft("draft1")
    assert calls == ["POST"] and raised.value.reason == "rateLimitExceeded"


def _msg(mid, labels=("INBOX", "UNREAD"), text="body", when=None):
    import time
    return {"id": mid, "threadId": "t" + mid, "labelIds": list(labels),
            "internalDate": str(int((when or time.time()) * 1000)),
            "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "S " + mid}],
                        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


class _SyncClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, **params):
        self.calls.append(path)
        value = self.routes[path]
        value = value(params) if callable(value) else value
        if isinstance(value, Exception):
            raise value
        return value


def test_sync_keeps_pages_already_saved_when_a_later_page_fails(tmp_path):
    store = gmail.Store(tmp_path / "gmail.db")
    client = _SyncClient({"profile": {"emailAddress": "me@example.edu", "historyId": "h1"},
                          "messages": lambda p: {"messages": [{"id": "a"}], "nextPageToken": "p2"}
                          if not p.get("pageToken") else {"messages": [{"id": "b"}]},
                          "messages/a": _msg("a"), "messages/b": gmail.GmailHTTPError(503)})
    with pytest.raises(gmail.GmailHTTPError):
        gmail.sync(client, store, page_limit=2)
    assert store.status()["cached_messages"] == 1 and store.state("page_token") == "p2"
    client.routes["messages/b"] = _msg("b")
    assert gmail.sync(client, store, page_limit=2)["status"] == "ok"
    assert store.status()["cached_messages"] == 2 and store.state("history_id") == "h1"
    store.close()


def test_history_label_changes_and_deletions_need_no_download(tmp_path):
    store = gmail.Store(tmp_path / "gmail.db")
    store.put(_msg("a"))
    store.put(_msg("b"))
    for key, value in {"account": "me@example.edu", "query": "newer_than:90d", "history_id": "h1",
                       "initial_pending": "0"}.items():
        store.set_state(key, value)
    store.con.commit()
    client = _SyncClient({"profile": {"emailAddress": "me@example.edu", "historyId": "h2"},
                          "history": {"historyId": "h2", "history": [
                              {"labelsRemoved": [{"message": {"id": "a", "labelIds": ["INBOX"]}}]},
                              {"messagesDeleted": [{"message": {"id": "b"}}]},
                              {"messagesAdded": [{"message": {"id": "c"}}]}]},
                          "messages/c": _msg("c")})
    assert gmail.sync(client, store)["status"] == "ok"
    assert [c for c in client.calls if c.startswith("messages/")] == ["messages/c"]
    rows = {r["id"]: r for r in store.search("")["items"]}
    assert rows["a"]["unread"] is False and rows["a"]["in_inbox"] is True
    assert "b" not in rows and "c" in rows
    store.close()


def test_label_event_without_label_list_downloads_instead_of_wiping(tmp_path):
    changes = gmail._history_changes([{"labelsAdded": [{"message": {"id": "a"}}]}])
    assert changes == {"a": ("fetch", None)}


def test_inbox_todo_lists_recent_unread_school_mail_only(tmp_path):
    import time
    store = gmail.Store(tmp_path / "gmail.db")
    store.put(_msg("todo"))
    store.put(_msg("read", labels=("INBOX",)))
    store.put(_msg("promo", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")))
    store.put(_msg("archived", labels=("UNREAD",)))
    store.put(_msg("old", when=time.time() - 30 * 86400))
    store.con.commit()
    todo = store.inbox_todo(days=7)
    assert [item["id"] for item in todo["items"]] == ["todo"] and todo["unread_count"] == 1
    store.close()


def test_search_results_carry_date_flags_and_attachment_count(tmp_path):
    store = gmail.Store(tmp_path / "gmail.db")
    message = _msg("a", labels=("INBOX", "UNREAD", "STARRED", "CATEGORY_UPDATES"))
    message["payload"] = {"mimeType": "multipart/mixed", "headers": message["payload"]["headers"], "parts": [
        message["payload"], {"partId": "1", "mimeType": "application/pdf", "filename": "f.pdf",
                             "body": {"attachmentId": "x", "size": 10}}]}
    store.put(message)
    store.con.commit()
    item = store.search("")["items"][0]
    assert item["unread"] and item["starred"] and item["in_inbox"] and item["category"] == "updates"
    assert item["attachments"] == 1 and item["date"][:4].isdigit()
    store.close()
