"""故障分类、旧索引、并发状态、重复导出和恢复：全部使用合成数据。"""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import Message
from types import SimpleNamespace

import pytest

from monash_study_kit import changes, content_index as CI, export, jobs, local_state
from monash_study_kit import moodlelib as ML, paths, textextract
from monash_study_kit import mcp_server as M


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_server_errors_do_not_request_login(status, monkeypatch):
    client = ML.MoodleClient(jar={"MoodleSession": "synthetic"}, persist=False)
    client._sesskey = "synthetic"
    monkeypatch.setattr(client, "_request", lambda *a, **k: ML.Response(status, "", {}, b"<html>Unavailable</html>"))
    with pytest.raises(ML.MoodleError):
        client.call("core_session_time_remaining")


def test_confirmed_auth_failure_still_requests_login(monkeypatch):
    client = ML.MoodleClient(jar={}, persist=False)
    client._sesskey = "synthetic"
    body = json.dumps([{"error": True, "exception": {"errorcode": "invalidsesskey"}}]).encode()
    monkeypatch.setattr(client, "_request", lambda *a, **k: ML.Response(200, "", {}, body))
    with pytest.raises(ML.MoodleAuthError):
        client.time_remaining()


def test_read_ajax_retries_503_with_retry_after(monkeypatch):
    client = ML.MoodleClient(jar={}, persist=False)
    client._sesskey = "synthetic"
    h = Message()
    h["Retry-After"] = "4"
    responses = iter([ML.Response(503, "", h, b"no"), ML.Response(200, "", h, b'[{"data":{"timeremaining":42}}]')])
    monkeypatch.setattr(client, "_request_once", lambda *a, **k: next(responses))
    waits = []
    monkeypatch.setattr(ML.time, "sleep", waits.append)
    assert client.time_remaining() == 42
    assert waits == [4]


def test_form_posts_are_not_replayed_and_get_retries_are_bounded(monkeypatch):
    client = ML.MoodleClient(jar={}, persist=False)
    calls = []
    monkeypatch.setattr(client, "_request_once", lambda *a, **k: calls.append(k) or ML.Response(503, "", {}, b"no"))
    monkeypatch.setattr(ML.time, "sleep", lambda _: None)
    client._request("https://synthetic.invalid/form", data=b"form")
    assert len(calls) == 1
    client._request("https://synthetic.invalid/read")
    assert len(calls) == 4


def indexed(tmp_path, monkeypatch):
    p = tmp_path / "FIT0000" / "a.md"
    p.parent.mkdir()
    p.write_text("old assignment requirements")
    monkeypatch.setattr(CI, "FILES_DIR", tmp_path)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    monkeypatch.setattr(CI, "_removed", lambda: set())
    con = sqlite3.connect(":memory:")
    CI.index(con)
    return p, con


def test_failed_reindex_marks_old_results_stale_and_recovers(tmp_path, monkeypatch):
    p, con = indexed(tmp_path, monkeypatch)
    p.write_text("new assignment requirements updated")
    real = CI.extract
    monkeypatch.setattr(CI, "extract", lambda _: (_ for _ in ()).throw(ValueError("broken")))
    stats = CI.index(con)
    assert stats["failed"] == 1 and stats["failures"][0]["path"] == str(p)
    hit = CI.search(con, "old")[0]
    assert hit["stale"] and hit["indexed_at"] and "上次" in hit["warning"]
    monkeypatch.setattr(CI, "extract", real)
    assert CI.index(con)["failed"] == 0
    assert CI.search(con, "old") == []
    assert not CI.search(con, "updated")[0].get("stale")


def test_selective_reindex_keeps_other_documents(tmp_path, monkeypatch):
    p, con = indexed(tmp_path, monkeypatch)
    other = p.parent / "b.md"
    other.write_text("other material")
    CI.index(con)
    p.write_text("replacement assignment")
    assert CI.index(con, only_paths={str(p)})["indexed"] == 1
    assert CI.search(con, "other")


def test_status_updates_merge_across_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATUS_FILE", tmp_path / "status.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: jobs.update_status({str(n): n}), range(40)))
    assert jobs.read_status() == {str(n): n for n in range(40)}
    assert not list(tmp_path.glob("*.tmp"))


def test_status_updates_merge_across_processes(tmp_path):
    code = ("from pathlib import Path; from monash_study_kit import jobs; import sys; "
            "jobs.STATUS_FILE=Path(sys.argv[1]); jobs.update_status({sys.argv[2]:True})")
    dest = tmp_path / "status.json"
    children = [subprocess.Popen([sys.executable, "-c", code, str(dest), str(n)]) for n in range(5)]
    assert all(p.wait(timeout=30) == 0 for p in children)
    assert json.loads(dest.read_text()) == {str(n): True for n in range(5)}


def test_atomic_write_failure_keeps_previous_data_and_cleans_temp(tmp_path, monkeypatch):
    dest = tmp_path / "state"
    dest.write_text("old")
    monkeypatch.setattr(local_state.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        local_state.atomic_write(dest, "new")
    assert dest.read_text() == "old" and list(tmp_path.iterdir()) == [dest]


def test_directory_export_cleans_only_owned_unmodified_files(tmp_path):
    p = export.Plan("FIT0000", entries=[("FIT0000/old.md", "old"), ("FIT0000/edited.md", "original")])
    export.write_dir(p, tmp_path)
    (tmp_path / "FIT0000/edited.md").write_text("my notes")
    (tmp_path / "FIT0000/personal.md").write_text("personal")
    stats = export.write_dir(export.Plan("FIT0000", entries=[("FIT0000/current.md", "current")]), tmp_path)
    assert not (tmp_path / "FIT0000/old.md").exists()
    assert (tmp_path / "FIT0000/edited.md").read_text() == "my notes"
    assert (tmp_path / "FIT0000/personal.md").read_text() == "personal"
    assert stats["preserved_modified_files"] == ["FIT0000/edited.md"]


def test_directory_export_rejects_escape_and_does_not_trust_manifest(tmp_path):
    (tmp_path / "outside.md").write_text("keep")
    p = export.Plan("FIT0000", entries=[("FIT0000/../outside.md", "replace")])
    with pytest.raises(ValueError):
        export.write_dir(p, tmp_path)
    assert (tmp_path / "outside.md").read_text() == "keep"


def test_old_directory_export_without_manifest_preserves_untracked_files(tmp_path):
    (tmp_path / "FIT0000").mkdir()
    (tmp_path / "FIT0000/legacy.md").write_text("untracked")
    export.write_dir(export.Plan("FIT0000", entries=[("FIT0000/new.md", "new")]), tmp_path)
    assert (tmp_path / "FIT0000/legacy.md").exists()


def test_pdf_extracts_only_selected_pages_and_cache_invalidates(tmp_path, monkeypatch):
    import pypdf
    p = tmp_path / "slides.pdf"
    p.write_bytes(b"synthetic PDF")
    calls = []
    class Page:
        def __init__(self, n):
            self.n = n
        def extract_text(self):
            calls.append(self.n)
            return f"page {self.n}"
    monkeypatch.setattr(pypdf, "PdfReader", lambda _: SimpleNamespace(is_encrypted=False, pages=[Page(i) for i in range(1, 6)]))
    monkeypatch.setattr(paths, "load_settings", lambda: {"pdf_extractor": "pypdf"})
    textextract._pdf_cache.clear()
    assert textextract.pdf_pages(p, 2, 3) == ["page 2", "page 3"]
    assert textextract.pdf_pages(p, 2, 3) == ["page 2", "page 3"]
    assert calls == [2, 3]
    p.write_bytes(b"a different synthetic PDF")
    textextract.pdf_pages(p, 2, 3)
    assert calls == [2, 3, 2, 3]


def test_mcp_pdf_page_numbers_and_invalid_ranges(tmp_path, monkeypatch):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"synthetic")
    monkeypatch.setattr(CI, "resolve_readable", lambda _: p)
    monkeypatch.setattr(textextract, "pdf_pages", lambda path, start, end: ["selected"])
    assert "第 7 页" in M.t_read_file({"path": "a.pdf", "page_start": 7})
    for args in ({"page_start": 0}, {"page_start": -1}, {"page_start": 3, "page_end": 2}, {"page_start": 1, "page_end": 101}):
        with pytest.raises(ValueError):
            M.t_read_file({"path": "a.pdf", **args})


def test_iana_timezone_handles_dst_and_legacy_offset(monkeypatch):
    monkeypatch.setattr(paths, "load_settings", lambda: {"timezone": "Australia/Melbourne", "tz_offset": 8})
    tz = paths.page_timezone()
    assert datetime(2026, 1, 1, tzinfo=tz).utcoffset().total_seconds() == 11 * 3600
    assert datetime(2026, 7, 1, tzinfo=tz).utcoffset().total_seconds() == 10 * 3600
    monkeypatch.setattr(paths, "load_settings", lambda: {"timezone": "", "tz_offset": 9.5})
    assert datetime(2026, 1, 1, tzinfo=paths.page_timezone()).utcoffset().total_seconds() == 9.5 * 3600


def test_changes_baseline_dedup_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "DB", tmp_path / "changes.db")
    monkeypatch.setattr(changes, "load_settings", lambda: {})
    base = {"key": "a", "fingerprint": "v1", "kind": "assignment", "course": "FIT0000", "title": "A1", "url": "https://example.invalid/a"}
    changes.observe("course", [base])
    assert changes.recent("2000-01-01T00:00:00Z") == []
    changes.observe("course", [{**base, "fingerprint": "v2"}])
    changes.observe("course", [{**base, "fingerprint": "v2"}])
    items = changes.recent("2000-01-01T00:00:00Z")
    assert len(items) == 1 and items[0]["kind"] == "assignment_changed" and items[0]["url"]
    assert "fingerprint" not in items[0]
    changes.observe("course", [])  # 不完整抓取不能制造下架事件
    assert len(changes.recent("2000-01-01T00:00:00Z")) == 1


def test_deadline_changes_have_old_new_time_and_materials_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "DB", tmp_path / "changes.db")
    monkeypatch.setattr(changes, "load_settings", lambda: {})
    item = dict(id=1, course="FIT0000", name="A1", due="2026-11-01", due_ts=1, source="api")
    changes.deadlines([item])
    changes.deadlines([{**item, "due": "2026-11-02", "due_ts": 2}])
    event = changes.recent("2000-01-01T00:00:00Z")[0]
    assert event["before"] == "2026-11-01" and event["after"] == "2026-11-02"
    monkeypatch.setattr(changes, "load_settings", lambda: {"materials_only": True})
    assert changes.recent("2000-01-01T00:00:00Z") == []


def test_background_network_failure_retries_soon_and_materials_skip(monkeypatch):
    from monash_study_kit import update_check
    monkeypatch.setattr(update_check, "enabled", lambda: False)
    monkeypatch.setattr(jobs, "hours_since_sync", lambda: 0)
    monkeypatch.setattr(jobs, "update_status", lambda v: None)
    monkeypatch.setattr(M, "load_settings", lambda: {})
    monkeypatch.setattr(M, "start_mail_sync", lambda: None)
    monkeypatch.setattr(M, "start_attendance_work", lambda: None)
    monkeypatch.setattr(jobs, "keepalive", lambda: (_ for _ in ()).throw(OSError("offline")))
    before = M.time.time()
    last = M._background_tick(3600, 1200, 0)
    assert before - 1141 < last < before - 1139
    monkeypatch.setattr(M, "load_settings", lambda: {"materials_only": True})
    assert M._background_tick(3600, 1200, 0) == 0


def test_recovery_only_uses_headless_login_and_cooldown(tmp_path, monkeypatch):
    from monash_study_kit import browser_login as BL, session_recovery as SR
    profile = tmp_path / "browser-profile"
    (profile / "Default").mkdir(parents=True)
    monkeypatch.setattr(SR, "BROWSER_PROFILE", profile)
    monkeypatch.setattr(jobs, "STATUS_FILE", tmp_path / "status.json")
    calls = []
    def login(**kw):
        calls.append(kw)
        raise BL.LoginFailed("MFA required")
    monkeypatch.setattr(BL, "ensure_login", login)
    for _ in range(2):
        SR._guard.acquire()
        SR._recover(lambda _: None)
    assert len(calls) == 1 and calls[0]["allow_window"] is False
    assert jobs.read_status()["session"]["state"] == "needs_login"


def test_recovery_disabled_in_materials_or_by_setting(monkeypatch):
    from monash_study_kit import session_recovery as SR
    monkeypatch.setattr(SR, "load_settings", lambda: {"auto_relogin": False})
    assert SR.start(lambda _: None) is False
    monkeypatch.setattr(SR, "load_settings", lambda: {"materials_only": True})
    assert SR.start(lambda _: None) is False


def test_partial_sync_keeps_last_complete_and_targeted_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(jobs, "_lock", lambda: True)
    monkeypatch.setattr(jobs, "_unlock", lambda: None)
    monkeypatch.setattr(jobs, "have_ed_token", lambda: True)
    monkeypatch.setattr(jobs, "load_settings", lambda: {"web_notes": False})
    monkeypatch.setattr(jobs, "sync_ed", lambda *a, **kw: {"failed": 1, "failures": [{"thread_id": 7, "course_id": 1}]})
    monkeypatch.setattr(jobs, "sync_moodle", lambda *a, **kw: {"errors": 0})
    monkeypatch.setattr(CI, "index", lambda *a, **kw: {"failed": 0})
    jobs.update_status({"last_complete_sync": "previous", "session": {"alive": True}})
    st = jobs.sync_all(log=lambda _: None)
    assert st["sync_state"] == "partial" and st["last_complete_sync"] == "previous"
    assert st["session"]["alive"]
    calls = []
    monkeypatch.setattr(jobs, "sync_ed", lambda *a, **kw: calls.append(kw) or {"failed": 0, "failures": []})
    monkeypatch.setattr(jobs, "sync_moodle", lambda *a, **kw: pytest.fail("成功的 Moodle 不应重试"))
    monkeypatch.setattr(CI, "index", lambda *a, **kw: pytest.fail("成功的索引不应重试"))
    st = jobs.sync_all(log=lambda _: None, retry_failed=True)
    assert calls == [{"retry_failures": [{"thread_id": 7, "course_id": 1}]}]
    assert st["sync_state"] == "complete"


def test_selective_moodle_retry_never_prunes_other_activities(tmp_path, monkeypatch):
    from monash_study_kit import syncer
    state = {"cm": [{"id": 1, "sectionnumber": 1}, {"id": 2, "sectionnumber": 1}],
             "section": [{"number": 1, "cmlist": [1, 2]}]}
    c = SimpleNamespace(course_state=lambda _: state)
    con = SimpleNamespace(execute=lambda *a: pytest.fail("不能清理完整课程状态"))
    sync = syncer.CourseSync(c, con, {"id": 1, "fullname": "FIT0000 Synthetic"}, only_cmids={2})
    monkeypatch.setattr(syncer, "plan_layout", lambda _: SimpleNamespace(folders={1: "Week 1"}, titles={1: "Week 1"}))
    seen = []
    monkeypatch.setattr(sync, "handle_cm", lambda cm, *a: seen.append(cm["id"]))
    monkeypatch.setattr(sync, "scan_section_pages", lambda *a: pytest.fail("不能扫描所有页面"))
    monkeypatch.setattr(sync, "mark_removed", lambda: pytest.fail("不能把未重试的文件标成下架"))
    sync.run()
    assert seen == [2]


def test_ed_failures_are_reported_and_retry_only_fetches_requested_threads(tmp_path, monkeypatch):
    from monash_study_kit import edlib, edsync
    db = tmp_path / "ed.db"
    real = edlib.db_connect
    monkeypatch.setattr(edsync, "db_connect", lambda: real(db))
    monkeypatch.setattr(edsync, "upsert_courses", lambda con, _: (
        con.execute("INSERT OR IGNORE INTO courses(id,code,tracked) VALUES(1,'FIT0000',1)"), con.commit()))
    monkeypatch.setattr(edsync, "auto_track", lambda *a: None)
    monkeypatch.setattr(edsync, "my_user_id", lambda _: 1)
    monkeypatch.setattr(edlib, "save_thread_state", lambda *a: None)
    monkeypatch.setattr(edsync, "store_thread", lambda *a: (1, 0))
    got = []
    class Client:
        def courses(self):
            return []
        def threads(self, _):
            return [{"id": 1}, {"id": 2}]
        def thread(self, tid):
            got.append(tid)
            raise OSError("synthetic offline")
    result = edsync.sync(client=Client(), log=lambda _: None, only_threads={2})
    assert got == [2] and result["failed"] == 1
    assert result["failures"][0]["thread_id"] == 2
    conn = real(db)
    assert conn.execute("SELECT note FROM runs").fetchone()[0] == "partial"
    conn.close()


def test_session_cookie_rotation_discards_cached_sesskey():
    client = ML.MoodleClient(jar={"MoodleSession": "old"}, persist=False)
    client._sesskey = "stale"
    h = Message()
    h["Set-Cookie"] = "MoodleSession=new; Secure; HttpOnly"
    client._absorb_cookies(h)
    assert client._sesskey is None and client.jar["MoodleSession"] == "new"


def test_staff_reply_change_summary_excludes_student_replies(tmp_path, monkeypatch):
    from monash_study_kit import edlib
    monkeypatch.setattr(changes, "DB", tmp_path / "changes.db")
    monkeypatch.setattr(changes, "load_settings", lambda: {})
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses(id,code) VALUES(1,'FIT0000')")
    conn.execute("INSERT INTO replies(id,thread_id,is_staff,text) VALUES(1,7,1,'Original answer')")
    conn.execute("INSERT INTO replies(id,thread_id,is_staff,text) VALUES(2,7,0,'Student answer')")
    changes.staff(conn, 7, 1, "A1 clarification", "https://example.invalid/7", False, "")
    conn.execute("UPDATE replies SET text='Corrected answer' WHERE id=1")
    conn.execute("UPDATE replies SET text='Different student answer' WHERE id=2")
    changes.staff(conn, 7, 1, "A1 clarification", "https://example.invalid/7", False, "")
    items = changes.recent("2000-01-01T00:00:00Z")
    assert len(items) == 1 and items[0]["kind"] == "staff_reply_changed"
    conn.close()


def test_empty_text_update_removes_old_search_results(tmp_path, monkeypatch):
    p, con = indexed(tmp_path, monkeypatch)
    p.write_text("")
    assert CI.index(con)["failed"] == 0
    assert CI.search(con, "assignment") == []


def test_real_ed_user_list_shape_is_supported_in_change_tracking(tmp_path, monkeypatch):
    from monash_study_kit import edlib, edsync
    monkeypatch.setattr(changes, "DB", tmp_path / "changes.db")
    monkeypatch.setattr(changes, "load_settings", lambda: {})
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses(id,code) VALUES(1,'FIT0000')")
    t = {"id": 7, "user_id": 2, "title": "Clarification", "type": "post"}
    full = {"thread": {"content": "<document><paragraph>Original answer</paragraph></document>"},
            "users": [{"id": 2, "name": "Synthetic teacher", "course_role": "staff"}]}
    edsync.store_thread(conn, 1, t, full, "v1")
    full["thread"]["content"] = "<document><paragraph>Corrected answer</paragraph></document>"
    edsync.store_thread(conn, 1, t, full, "v2", prev={"content_hash": "v1"})
    event = changes.recent("2000-01-01T00:00:00Z")[0]
    assert event["kind"] == "staff_post_changed"
    conn.close()


def test_changes_command_json_and_invalid_timezone(monkeypatch, tmp_path, capsys):
    from monash_study_kit import cli
    monkeypatch.setattr(changes, "DB", tmp_path / "empty.db")
    assert cli.main(["changes", "--days", "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    with pytest.raises(ValueError):
        cli.parse_setting("timezone", "Synthetic/Invalid", "")


def test_successful_material_retry_runs_incremental_index(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(jobs, "_lock", lambda: True)
    monkeypatch.setattr(jobs, "_unlock", lambda: None)
    monkeypatch.setattr(jobs, "have_ed_token", lambda: False)
    monkeypatch.setattr(jobs, "load_settings", lambda: {"web_notes": False, "use_ed": False})
    jobs.update_status({"moodle": {"errors": 1, "failures": [{"course_id": 1, "cmid": 2}]}, "index": {"failed": 0}})
    monkeypatch.setattr(jobs, "sync_moodle", lambda *a, **k: {"errors": 0})
    called = []
    monkeypatch.setattr(CI, "index", lambda *a, **k: called.append(k) or {"failed": 0})
    assert jobs.sync_all(log=lambda _: None, retry_failed=True)["sync_state"] == "complete"
    assert called == [{}]
