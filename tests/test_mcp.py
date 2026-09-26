"""MCP 协议层：握手、工具清单、只读边界、错误提示、读文件不越界。"""
import json

from monash_study_kit import mcp_server as M
from monash_study_kit.moodlelib import MoodleAuthError


def call(method, params=None, mid=1):
    return M.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_initialize_and_notifications():
    r = call("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert r["serverInfo"]["name"] == "monash" and "tools" in r["capabilities"]
    assert "monash_login" in r["instructions"]
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_cover_both_sides_and_only_login_sync_are_not_read_only():
    tools = {t["name"]: t for t in call("tools/list")["result"]["tools"]}
    assert {"study_todo", "moodle_due", "read_file", "search_content", "ed_updates", "ed_thread", "monash_status"} <= set(tools)
    assert not any("submit" in n or n == "moodle_quiz" or "post" in n for n in tools)
    writable = {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]}
    assert writable == {"monash_login", "monash_sync"}
    assert tools["read_file"]["inputSchema"]["required"] == ["path"]


def test_unknown_tool_and_method():
    assert call("tools/call", {"name": "moodle_submit"})["error"]["code"] == -32602
    assert call("resources/list")["error"]["code"] == -32601


def test_auth_errors_tell_claude_what_to_do(monkeypatch):
    def dead(args):
        raise MoodleAuthError("expired")
    monkeypatch.setitem(M.TOOLS, "moodle_grades", (dead, "", {}))
    r = call("tools/call", {"name": "moodle_grades"})["result"]
    assert r["isError"] and "monash_login" in r["content"][0]["text"]

    from monash_study_kit.edlib import EdAuthError

    def no_token(args):
        raise EdAuthError("no token")
    monkeypatch.setitem(M.TOOLS, "ed_updates", (no_token, "", {}))
    r = call("tools/call", {"name": "ed_updates"})["result"]
    assert r["isError"] and "monash login ed" in r["content"][0]["text"]


def test_read_file_stays_inside_files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "FILES_DIR", tmp_path)
    (tmp_path / "U").mkdir()
    (tmp_path / "U" / "a.py").write_text("x" * 50, encoding="utf-8")
    r = call("tools/call", {"name": "read_file", "arguments": {"path": "U/a.py", "max_chars": 10}})["result"]
    assert r["content"][0]["text"].startswith("x" * 10) and "offset=10" in r["content"][0]["text"]
    r = call("tools/call", {"name": "read_file", "arguments": {"path": "U\\a.py"}})["result"]
    assert not r.get("isError")          # Windows 风格的路径也认
    r = call("tools/call", {"name": "read_file", "arguments": {"path": "../etc/passwd"}})["result"]
    assert r["isError"]


def test_list_files_explains_when_nothing_synced_yet():
    r = call("tools/call", {"name": "list_files", "arguments": {}})["result"]
    assert "还没同步过" in json.loads(r["content"][0]["text"])["note"]


def test_client_is_rebuilt_after_relogin(tmp_path, monkeypatch):
    from monash_study_kit import moodlelib
    cookie = tmp_path / "cookie"
    monkeypatch.setattr(M, "COOKIE_FILE", cookie)
    monkeypatch.setattr(moodlelib, "COOKIE_FILE", cookie)
    monkeypatch.setattr(M, "_client", None)
    cookie.write_text(json.dumps({"MoodleSession": "old"}), encoding="utf-8")
    assert M.client().jar["MoodleSession"] == "old"
    import os
    cookie.write_text(json.dumps({"MoodleSession": "new"}), encoding="utf-8")
    os.utime(cookie, (1, 1))                 # mtime 变了 → 换新客户端
    assert M.client().jar["MoodleSession"] == "new"


def test_catalog_stays_small():
    """工具清单每次对话都整份发给 Claude，不调用也算额度。加工具/改说明时别让它涨回去。"""
    tools = call("tools/list")["result"]["tools"]
    size = len(json.dumps(tools, ensure_ascii=False))
    assert len(tools) <= 17 and size <= 5600, (len(tools), size)
    assert len(M.INSTRUCTIONS) <= 300
    enums = tools[[t["name"] for t in tools].index("ed_threads")]["inputSchema"]["properties"]["only"]["enum"]
    assert set(enums) == {"starred", "watching", "unseen", "mine", "unread_replies"}


def _ed_fixture(tmp_path, monkeypatch):
    from monash_study_kit import edlib
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses (id, code, name, tracked) VALUES (1, 'FIT2102', 'PP', 1)")
    rows = [(10, "question", "Monad laws?", 1, 0), (11, "post", "Monad tutorial", 0, 1),
            (12, "question", "Unrelated", 1, 1)]
    for tid, typ, title, seen, mine in rows:
        conn.execute("INSERT INTO threads (id, course_id, number, type, title, body, created_at, updated_at, url,"
                     " first_seen, reply_count, is_pinned, is_private, is_answered, is_staff_answered)"
                     " VALUES (?,1,?,?,?,'x','2026-09-20T10:00:00+10:00','2026-09-20T10:00:00+10:00','u',"
                     " '2026-09-20T00:00:00+00:00',0,0,0,0,0)", (tid, tid, typ, title))
        conn.execute("INSERT INTO thread_state VALUES (?,0,0,?,0,NULL,?,'now')", (tid, seen, mine))
    conn.commit()
    monkeypatch.setattr(M, "_ed", lambda: edlib.db_connect(tmp_path / "ed.db"))
    return conn


def test_ed_threads_search_and_only_filters(tmp_path, monkeypatch):
    _ed_fixture(tmp_path, monkeypatch)

    def refs(**a):
        r = call("tools/call", {"name": "ed_threads", "arguments": a})["result"]
        assert not r.get("isError"), r
        return sorted(t["ref"] for t in json.loads(r["content"][0]["text"]))
    assert refs(query="monad") == ["FIT2102#10", "FIT2102#11"]
    assert refs(query="monad", only="unseen") == ["FIT2102#11"]        # is_seen=0 的才算没点开过
    assert refs(query="monad", only="mine") == ["FIT2102#11"]
    assert refs(only="mine") == ["FIT2102#11", "FIT2102#12"]
    assert refs(type="question", unanswered=True) == ["FIT2102#10", "FIT2102#12"]
    r = call("tools/call", {"name": "ed_threads", "arguments": {"only": "bogus"}})["result"]
    assert r["isError"]


def test_list_files_links_mode_needs_course():
    r = call("tools/call", {"name": "list_files", "arguments": {"links": True}})["result"]
    assert r["isError"] and "course" in r["content"][0]["text"]
