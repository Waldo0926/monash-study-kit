import base64
import json
import os

import pytest

from monash_study_kit import cli, gmail, gmail_tools, mcp_server, remote


def encoded(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def message(mid="a", text="Exchange deadline Friday", **extra):
    # 真实的毫秒时间戳。1970 年开头那一天在 Windows 上换不了本地时间（OSError 22），真实邮件也不会有
    return {"id": mid, "threadId": "thread1", "internalDate": "1761868800000", "labelIds": ["INBOX", "UNREAD"],
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "Subject", "value": "NTU application"},
                {"name": "From", "value": "abroad@example.edu"}], "body": {"data": encoded(text)}}, **extra}


class Fake:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        response = self.routes[endpoint]
        if callable(response):
            response = response(params)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def store(tmp_path):
    out = gmail.Store(tmp_path / "private" / "gmail.db")
    yield out
    out.close()


def test_sync_durable_offline_search_and_no_read_mutations(store):
    client = Fake({"profile": {"emailAddress": "ME@EXAMPLE.EDU", "historyId": "h1"},
                   "messages": {"messages": [{"id": "a"}]}, "messages/a": message()})
    result = gmail.sync(client, store)
    assert result["status"] == "ok" and result["cached_messages"] == 1
    assert store.search("Exchange Friday")["items"][0]["id"] == "a"
    assert json.loads(store.con.execute("SELECT labels FROM messages").fetchone()[0]) == ["INBOX", "UNREAD"]
    reopened = gmail.Store(store.path)
    try:
        assert "Friday" in reopened.read_thread("thread1")["text"]
        assert reopened.status()["account"] == "me@example.edu"
    finally:
        reopened.close()
    assert not any("modify" in endpoint for endpoint, _ in client.calls)
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_mime_html_and_attachment_metadata():
    msg = message()
    msg["payload"].update(mimeType="multipart/mixed", body={}, parts=[
        {"mimeType": "text/html", "body": {"data": encoded("<style>secret-css</style><p>Hello<br>World</p>")}},
        {"mimeType": "application/pdf", "partId": "2", "filename": "../spec.pdf",
         "body": {"attachmentId": "file1", "size": 4}}])
    content = gmail.message_content(msg)
    assert "Hello\nWorld" in content["body"] and "secret-css" not in content["body"]
    assert content["attachments"][0]["filename"] == "../spec.pdf"


def test_initial_pagination_resumes_with_original_baseline(store):
    routes = {"profile": {"emailAddress": "me@example.edu", "historyId": "h1"},
              "messages": lambda p: {"messages": [{"id": "a"}], "nextPageToken": "page2"}
              if not p["pageToken"] else {"messages": [{"id": "b"}]},
              "messages/a": message(), "messages/b": message("b", "Older message")}
    client = Fake(routes)
    assert gmail.sync(client, store, page_limit=1)["status"] == "more_pending"
    assert store.status()["initial_sync_pending"]
    routes["profile"]["historyId"] = "h2"
    assert gmail.sync(client, store, page_limit=1)["status"] == "ok"
    assert store.state("history_id") == "h1"
    assert store.status()["cached_messages"] == 2


def test_failure_inside_a_page_saves_nothing_from_it_and_retry_resumes(store):
    # 每页和它的游标一起提交：失败那页一封都不存，游标停在那页开头，下次从这里接着下。
    client = Fake({"profile": {"emailAddress": "me@example.edu", "historyId": "h1"},
                   "messages": {"messages": [{"id": "a"}, {"id": "b"}]},
                   "messages/a": message(), "messages/b": gmail.GmailHTTPError(503)})
    with pytest.raises(gmail.GmailHTTPError):
        gmail.sync(client, store)
    assert store.status()["cached_messages"] == 0
    assert store.state("page_token") == "" and store.state("initial_pending") == "1"
    assert not store.state("history_id")
    client.routes["messages/b"] = message("b")
    assert gmail.sync(client, store)["status"] == "ok"
    assert store.status()["cached_messages"] == 2 and store.state("history_id") == "h1"


def seed(store):
    store.put(message())
    for key, value in {"account": "me@example.edu", "query": "newer_than:90d", "history_id": "h1"}.items():
        store.set_state(key, value)
    store.con.commit()


def test_incremental_labels_and_deletion(store):
    seed(store)
    routes = {"profile": {"emailAddress": "me@example.edu", "historyId": "h2"},
              "history": {"historyId": "h2", "history": [{"labelsRemoved": [{"message": {"id": "a"}}]}]},
              "messages/a": message(labelIds=["INBOX"])}
    assert gmail.sync(Fake(routes), store)["status"] == "ok"
    assert store.state("history_id") == "h2"
    routes["history"] = {"historyId": "h3", "history": [{"messagesDeleted": [{"message": {"id": "a"}}]}]}
    routes["messages/a"] = gmail.GmailHTTPError(404)
    gmail.sync(Fake(routes), store)
    assert not store.search("Exchange")["items"]
    with pytest.raises(LookupError):
        store.read_thread("thread1")


def test_expired_history_requires_rebuild(store):
    seed(store)
    result = gmail.sync(Fake({"profile": {"emailAddress": "me@example.edu", "historyId": "h2"},
                              "history": gmail.GmailHTTPError(404)}), store)
    assert result["status"] == "resync_required"
    assert store.state("history_id") == "" and store.status()["cached_messages"] == 1


def test_other_account_cannot_replace_or_download_cache(store):
    seed(store)
    with pytest.raises(gmail.GmailError, match="another mailbox"):
        gmail.sync(Fake({"profile": {"emailAddress": "other@example.edu", "historyId": "h2"}}), store)
    assert store.state("account") == "me@example.edu"


def test_attachment_download_is_durable_and_filename_cannot_escape(store):
    seed(store)
    msg = message()
    msg["payload"] = {"partId": "0", "mimeType": "application/pdf", "filename": "../../evil.pdf",
                      "body": {"attachmentId": "file1", "size": 4}}
    store.put(msg)
    store.con.commit()
    client = Fake({"profile": {"emailAddress": "me@example.edu"},
                   "messages/a/attachments/file1": {"data": encoded("test")}})
    result = gmail.cache_attachment(client, store, "a", "0")
    from pathlib import Path
    target = Path(result["path"])
    assert target.parent == store.path.parent / "attachments"
    assert target.read_bytes() == b"test" and target.name == result["sha256"]
    with pytest.raises(gmail.GmailError, match="size limit"):
        gmail.cache_attachment(client, store, "a", "0", max_bytes=3)
    assert gmail.cache_attachment(Fake({}), store, "a", "0")["cached"] is True
    target.unlink()
    with pytest.raises(gmail.GmailError, match="another mailbox"):
        gmail.cache_attachment(Fake({"profile": {"emailAddress": "other@example.edu"}}), store, "a", "0")


def test_large_text_body_downloads_through_attachment_api(store):
    msg = message()
    msg["payload"]["body"] = {"attachmentId": "largebody", "size": 1000}
    client = Fake({"profile": {"emailAddress": "me@example.edu", "historyId": "h1"},
                   "messages": {"messages": [{"id": "a"}]}, "messages/a": msg,
                   "messages/a/attachments/largebody": {"data": encoded("Large exchange application")}})
    gmail.sync(client, store)
    assert store.search("Large")["items"]
    assert store.read_thread("thread1")["messages"][0]["attachments"] == []


def test_fulltext_input_is_literal_and_pagination_is_explicit(store):
    seed(store)
    store.put(message("b"))
    store.con.commit()
    result = store.search("Exchange", limit=1)
    assert len(result["items"]) == 1 and result["next_offset"] == 1
    assert not store.search('" OR 1=1 --')["items"]


def test_readonly_token_cannot_modify_mailbox():
    class Credentials:
        valid = True
        scopes = [gmail.READ_SCOPE]
        token = "do-not-log"
    client = gmail.Client(credentials=Credentials())
    with pytest.raises(gmail.GmailAuthError, match="--manage"):
        client.send_draft("draft1")


def test_write_is_not_retried_after_server_failure(monkeypatch):
    class Credentials:
        valid = True
        scopes = [gmail.MANAGE_SCOPE]
        token = "do-not-log"
    calls = []
    class Opener:
        def open(self, req, **kw):
            calls.append(req)
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 503, "failed", {}, None)
    monkeypatch.setattr(gmail.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(gmail.GmailHTTPError):
        gmail.Client(credentials=Credentials()).send_draft("draft1")
    assert len(calls) == 1


def test_mcp_opt_in_and_old_remote_grants_do_not_gain_mailbox_access(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_settings", lambda: {"gmail_enabled": False})
    assert not (gmail_tools.TOOLS.keys() & {t["name"] for t in mcp_server.tool_list()})
    monkeypatch.setattr(mcp_server, "load_settings", lambda: {"gmail_enabled": True})
    assert gmail_tools.TOOLS.keys() <= {t["name"] for t in mcp_server.tool_list()}
    listed = remote.handle_remote({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert not (gmail_tools.TOOLS.keys() & {t["name"] for t in listed})
    result = remote.handle_remote({"id": 2, "method": "tools/call", "params": {"name": "search_gmail_cache"}})
    assert "error" in result


def test_draft_and_send_default_to_review_without_writing(monkeypatch, tmp_path, capsys, store):
    from monash_study_kit import cli_gmail
    eml = tmp_path / "draft.eml"
    eml.write_text("To: recipient@example.edu\nSubject: Preview\n\nHello")
    monkeypatch.setattr(cli_gmail.gmail, "Store", lambda: store)
    class NoWrites:
        def get(self, endpoint, **kwargs):
            return {"message": message()}
        def create_draft(self, *args):
            pytest.fail("Preview must not create a server draft")
        def send_draft(self, *args):
            pytest.fail("Preview must not send mail")
    monkeypatch.setattr(cli_gmail.gmail, "Client", NoWrites)
    # Store.close is invoked per command; keep this fixture open across both previews.
    monkeypatch.setattr(store, "close", lambda: None)
    args = cli.build_parser().parse_args(["gmail", "draft", "--file", str(eml)])
    args.fn(args)
    assert json.loads(capsys.readouterr().out)["committed"] is False
    args = cli.build_parser().parse_args(["gmail", "send-draft", "draft1"])
    args.fn(args)
    assert json.loads(capsys.readouterr().out)["committed"] is False
