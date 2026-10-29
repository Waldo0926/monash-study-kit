"""The three panels share existing tools and preserve their textual replies."""
import copy
import hashlib
import json

from monash_study_kit import edlib, edquery, lessons, mcp_server as M, widgets as W


def call(method, params=None):
    return M.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_original_17_tool_contracts_are_unchanged():
    # Complete canonical catalog from 1.3.0 (3a215112...), excluding only new UI metadata.
    original = [{k: v for k, v in t.items() if k != "_meta"}
                for t in M.tool_list() if t["name"] != "search_moodle_activities"]
    blob = json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert len(original) == 17
    assert hashlib.sha256(blob.encode()).hexdigest() == "2597d4badd279b2a12917025bbae3a6cffd18ad7fdd401a0ff7e56588daece84"


def test_panel_resource_is_packaged_self_contained_and_safe():
    assert call("resources/list")["result"]["resources"] == [W.resource()]
    html = call("resources/read", {"uri": W.URI})["result"]["contents"][0]["text"]
    assert W.VIEW_KEY in html and 'ev.source !== window.parent' in html
    assert "ui/message" in html and "ui/notifications/tool-result" in html
    assert "fetch(" not in html and "<script src=" not in html and "show_quiz" not in html
    assert "textContent" in html or "document.createElement" in html
    assert call("resources/read", {"uri": "file:///secrets"})["error"]["code"] == -32002
    assert call("resources/templates/list")["result"] == {"resourceTemplates": []}
    for tool in M.tool_list():
        assert ("_meta" in tool) == (tool["name"] in W.TOOLS)


def test_todo_panel_preserves_json_text(monkeypatch):
    data = {"days": 7, "due": [], "ed_lessons": [], "notes": ["offline fixture"]}
    before = copy.deepcopy(data)
    monkeypatch.setitem(M.TOOLS, "get_study_todo", (lambda a: data, "", {}))
    result = call("tools/call", {"name": "get_study_todo"})["result"]
    assert json.loads(result["content"][0]["text"]) == before
    assert result["_meta"][W.VIEW_KEY]["kind"] == "todo" and data == before


def test_real_lesson_progress_and_quiz_metadata(tmp_path, monkeypatch):
    conn = edlib.db_connect(tmp_path / "ed.db")
    lessons.ensure_lessons_schema(conn)
    conn.execute("INSERT INTO courses (id, code, name, tracked) VALUES (1, 'FIT2109', 'Workshop', 1)")
    conn.execute("INSERT INTO modules VALUES (1,1,'Week 3',NULL)")
    for i, status in ((7, "unattempted"), (8, "completed")):
        conn.execute("INSERT INTO lessons (id,course_id,module_id,idx,title,status) VALUES (?,1,1,0,?,?)", (i, f"W3 Lesson {i}", status))
    conn.execute("INSERT INTO quiz_questions (id,lesson_id,course_id,slide_id,idx,question,options,multiple,slide_title) "
                 "VALUES (1,7,1,70,0,'Choose A','[\"A\",\"B\"]',0,'Warm up')")
    conn.commit()
    monkeypatch.setattr(M, "_ed", lambda: conn)
    try:
        progress = call("tools/call", {"name": "get_ed_lessons", "arguments": {"course": "FIT2109"}})["result"]
        data = json.loads(progress["content"][0]["text"])
        assert len(data) == 2
        module = progress["_meta"][W.VIEW_KEY]["modules"][0]
        assert (module["total"], module["completed"]) == (2, 1)
        assert module["unfinished"][0]["has_quiz"]
        quiz = call("tools/call", {"name": "get_ed_lessons", "arguments": {"course": "FIT2109", "quiz": True}})["result"]
        assert quiz["content"][0]["text"] == edquery.quiz_markdown(edquery.quizzes(conn, "FIT2109"))
        view = quiz["_meta"][W.VIEW_KEY]
        assert view["kind"] == "quiz" and view["questions"][0]["options"] == ["A", "B"]
        assert conn.execute("SELECT status FROM lessons WHERE id=7").fetchone()[0] == "unattempted"
    finally:
        conn.close()


def test_panel_failure_keeps_successful_text(monkeypatch):
    monkeypatch.setitem(M.TOOLS, "get_study_todo", (lambda a: {"due": []}, "", {}))
    def unavailable(*a):
        raise ValueError("No panel")
    monkeypatch.setattr(W, "payload", unavailable)
    assert call("tools/call", {"name": "get_study_todo"})["result"] == {"content": [{"type": "text", "text": '{"due": []}'}]}


def test_activity_finder_limits_and_does_not_return_internal_score(monkeypatch):
    client = object()
    monkeypatch.setattr(M, "client", lambda: client)
    monkeypatch.setattr(M, "_tracked_or_current", lambda: [{"id": 1}])
    def find(c, query, courses, limit):
        assert c is client and query == "week 7" and courses == [{"id": 1}] and limit == 50
        return [{"id": 7, "url": "https://learning.monash.edu/mod/resource/view.php?id=7", "score": 3}]
    monkeypatch.setattr(M.F, "find", find)
    result = call("tools/call", {"name": "search_moodle_activities", "arguments": {"query": "week 7", "limit": 999}})["result"]
    assert "score" not in json.loads(result["content"][0]["text"])[0]
