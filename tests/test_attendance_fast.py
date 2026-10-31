"""不开浏览器读考勤、按课表自动同步、登录时长记录。不联网，用假页面。"""
import io
import json
import urllib.error
from datetime import datetime, timedelta

import pytest

attendance = pytest.importorskip("monash_study_kit.attendance")
from monash_study_kit import attendance_auto, attendance_http, htmldom  # noqa: E402
from monash_study_kit import mcp_server as M  # noqa: E402

ENTRY = attendance.ORIGIN + "/student/Entry.aspx?id=1"

UNITS_HTML = f'''<html><body>
<select id="daySel"><option value="30_Oct_26">Fri</option><option value="31_Oct_26" selected>Sat</option>
<option value="1_Nov_26">Sun</option></select>
<div id="dayPanel_30_Oct_26" style="display: none"><ul>
  <li>FIT2102 Workshop 01 2:00PM <span class="unrelated-success">status</span><a href="{ENTRY}">Enter</a></li>
  <li>FIT2102 Workshop 02 4:00PM <span class="glyphicon glyphicon-ok"></span></li>
  <li>FIT2109 Studio 01 2:00PM <span class="glyphicon-ok">✓</span></li>
  <li style="display:none">FIT9999 Tutorial 03 9:00AM <span class="glyphicon-ok"></span></li>
</ul></div>
<div id="dayPanel_31_Oct_26"><table><tr><td>FIT2109</td><td>Applied Class 2</td><td>9:00 AM</td>
  <td><a href="Entry.aspx?id=7">Enter code</a></td></tr></table></div>
</body></html>'''


def rows(key):
    panel = htmldom.parse(UNITS_HTML).find(id="dayPanel_" + key)
    return {(r["course"], r["session"]): r for r in attendance_http.parse_day(panel, key)}


def test_python_parser_reads_one_hidden_panel_like_the_extension():
    day = rows("30_Oct_26")
    assert set(day) == {("FIT2102", "Workshop 01"), ("FIT2102", "Workshop 02"), ("FIT2109", "Studio 01")}
    assert day["FIT2102", "Workshop 01"]["completed"] is False          # 真正的 Entry 链接说明还没签
    assert day["FIT2102", "Workshop 01"]["entryUrl"] == ENTRY
    assert day["FIT2102", "Workshop 02"]["completed"] is True
    assert day["FIT2109", "Studio 01"]["entryUrl"] == ""
    other = rows("31_Oct_26")
    assert other["FIT2109", "Applied Class 02"]["entryUrl"] == attendance.ORIGIN + "/student/Entry.aspx?id=7"
    assert other["FIT2109", "Applied Class 02"]["time"] == "9:00 AM"


def test_synthetic_completed_link_is_never_an_entry():
    html = ('<div id="p"><li>FIT2102 Lab 4 10:00AM <a href="https://x/student/Entry.aspx?mah_completed=1">'
            "x</a></li></div>")
    (row,) = attendance_http.parse_day(htmldom.parse(html).find(id="p"), "k")
    assert row["completed"] is True and row["entryUrl"] == ""


def fake_fetch(monkeypatch, html=UNITS_HTML, cookies=()):
    monkeypatch.setattr(attendance_http, "fetch", lambda url: (html, list(cookies)))
    monkeypatch.setattr(attendance_http, "update_cookies", lambda c: len(c))


def test_read_days_uses_one_request_and_reports_missing_days(monkeypatch):
    fake_fetch(monkeypatch)
    out = attendance_http.read_days(["30_Oct_26", "31_Oct_26", "2_Nov_26"])
    assert len(out["30_Oct_26"]) == 3 and len(out["31_Oct_26"]) == 1 and out["2_Nov_26"] is None


def test_read_days_gives_up_when_panels_are_loaded_on_demand(monkeypatch):
    fake_fetch(monkeypatch)
    with pytest.raises(attendance_http.FastPathUnavailable):
        attendance_http.read_days(["1_Nov_26"])          # 下拉框里有，HTML 里没有区块
    fake_fetch(monkeypatch, "<html><body>no menu</body></html>")
    with pytest.raises(attendance_http.FastPathUnavailable):
        attendance_http.read_days(["30_Oct_26"])


def state_file(monkeypatch, tmp_path, cookies):
    path = tmp_path / "attendance-state.json"
    path.write_text(json.dumps({"cookies": cookies, "origins": []}))
    monkeypatch.setattr(attendance_http, "STATE_FILE", path)
    return path


def test_fetch_treats_redirect_or_login_page_as_expired(monkeypatch, tmp_path):
    state_file(monkeypatch, tmp_path, [{"name": "S", "value": "v"}])
    sent = []

    class Opener:
        def __init__(self, outcome):
            self.outcome = outcome

        def open(self, request, timeout):
            sent.append(request.get_header("Cookie"))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    redirect = urllib.error.HTTPError(attendance_http.UNITS, 302, "Found", {"Location": "https://login"}, io.BytesIO())
    monkeypatch.setattr(attendance_http.urllib.request, "build_opener", lambda *a: Opener(redirect))
    with pytest.raises(attendance.AttendanceAuthError):
        attendance_http.fetch(attendance_http.UNITS)
    assert sent == ["S=v"]

    class Page(io.BytesIO):
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8", "get_all": lambda self, k: None})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(attendance_http.urllib.request, "build_opener", lambda *a: Opener(Page(b"Sign in to your account")))
    with pytest.raises(attendance.AttendanceAuthError):
        attendance_http.fetch(attendance_http.UNITS)
    with pytest.raises(ValueError):
        attendance_http.fetch("https://evil.example/student/x")


def test_new_cookies_from_the_portal_are_saved(monkeypatch, tmp_path):
    path = state_file(monkeypatch, tmp_path, [{"name": "keep", "value": "1", "domain": "d", "path": "/"},
                                              {"name": "renew", "value": "old", "domain": "d", "path": "/"},
                                              {"name": "gone", "value": "x", "domain": "d", "path": "/"}])
    changed = attendance_http.update_cookies([
        "renew=new; Path=/; Max-Age=3600; HttpOnly",
        "gone=; expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/",
        "fresh=abc; Path=/student/",
    ])
    cookies = {c["name"]: c for c in json.loads(path.read_text())["cookies"]}
    assert changed == 3 and set(cookies) == {"keep", "renew", "fresh"}
    assert cookies["renew"]["value"] == "new" and cookies["renew"]["domain"] == "d"
    assert cookies["fresh"]["path"] == "/student/"


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(attendance, "CACHE_FILE", tmp_path / "records.json")
    monkeypatch.setattr(attendance_auto, "SESSION_FILE", tmp_path / "session.json")
    return tmp_path


def test_sync_uses_fast_read_when_on_and_falls_back_to_the_browser(monkeypatch, cache):
    from monash_study_kit import paths
    monkeypatch.setattr(paths, "load_settings", lambda: {"attendance_fast_read": True})
    today = attendance._day_key(datetime.now(attendance.TZ).date())
    row = {"course": "FIT2102", "session": "Workshop 01", "time": "2:00PM", "entryUrl": "", "completed": True,
           "attendanceLabel": "x", "sourceUrl": attendance_http.UNITS}
    monkeypatch.setattr(attendance_http, "read_days", lambda keys: {k: [dict(row)] if k == today else [] for k in keys})
    monkeypatch.setattr(attendance, "_read_browser", lambda keys, script: pytest.fail("should not open a browser"))
    result = attendance.sync(1)
    assert result["method"] == "http" and result["read_records"] == 1
    assert attendance.status()["last_method"] == "http"

    def unavailable(keys):
        raise attendance_http.FastPathUnavailable("no panels")
    monkeypatch.setattr(attendance_http, "read_days", unavailable)
    monkeypatch.setattr(attendance, "_read_browser", lambda keys, script: {k: [] for k in keys})
    assert attendance.sync(1)["method"] == "browser"
    monkeypatch.setattr(paths, "load_settings", lambda: {"attendance_fast_read": False})
    monkeypatch.setattr(attendance_http, "read_days", lambda keys: pytest.fail("fast read was turned off"))
    assert attendance.sync(1)["method"] == "browser"
    assert paths.DEFAULTS["attendance_fast_read"] is True      # 门户上对照过，默认打开


def test_login_lifetime_is_measured_from_successes_and_the_first_failure(monkeypatch, cache):
    clock = [1000.0]
    monkeypatch.setattr(attendance_auto.time, "time", lambda: clock[0])
    assert attendance_auto.lifetime() == {"current": "还没记录到登录"}
    attendance_auto.record_login()
    clock[0] += 5 * 3600
    attendance_auto.record_ok()
    assert attendance_auto.lifetime()["current"] == {"still_valid_after_hours": 5.0}
    clock[0] += 3 * 3600
    attendance_auto.record_failure()
    clock[0] += 3600
    attendance_auto.record_failure()                       # 只记第一次失败
    assert attendance_auto.lifetime()["current"] == {"expired_after_hours": [5.0, 8.0]}
    attendance_auto.record_login()
    assert attendance_auto.lifetime()["past_logins"] == [{"expired_after_hours": [5.0, 8.0]}]


def test_sync_records_an_expired_login(monkeypatch, cache):
    def expired(keys, script):
        raise attendance.AttendanceAuthError("expired")
    monkeypatch.setattr(attendance, "_read_browser", expired)
    attendance_auto.record_login()
    with pytest.raises(attendance.AttendanceAuthError):
        attendance.sync(1)
    assert "expired_after_hours" in attendance_auto.lifetime()["current"]


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=attendance.TZ)   # 2026-11-02 是星期一


def record(day, time, completed=False):
    return {"date": f"2026-11-{day:02d}", "time": time, "completed": completed}


def test_schedule_runs_in_the_morning_and_after_unfinished_classes_only():
    classes = [record(2, "10:00AM"), record(2, "2:00PM", completed=True), record(3, "9:00AM")]
    assert attendance_auto.due(classes, None, at(2, 7, 59)) is None
    assert attendance_auto.due(classes, None, at(2, 8, 5)) == ("morning", 3)
    ran = at(2, 8, 5).timestamp()
    assert attendance_auto.due(classes, ran, at(2, 10, 10)) is None
    assert attendance_auto.due(classes, ran, at(2, 10, 16)) == ("after_class", 1)
    ran = at(2, 10, 16).timestamp()
    assert attendance_auto.due(classes, ran, at(2, 14, 30)) is None        # 已签到的课不用再看
    assert attendance_auto.due(classes, ran, at(2, 23, 0)) is None
    assert attendance_auto.due([record(7, "10:00AM")], None, at(7, 9)) is None       # 周六早上不跑
    assert attendance_auto.due([record(7, "10:00AM")], None, at(7, 10, 20)) == ("after_class", 1)
    # 电脑睡了一上午，醒来只补一次
    assert attendance_auto.due(classes, None, at(2, 12)) == ("after_class", 1)


def test_background_reads_on_schedule_and_keepalive_is_off_by_default(monkeypatch, cache):
    calls = []
    monkeypatch.setattr(M, "load_settings", lambda: {"attendance_enabled": True, "attendance_auto_sync": True})
    monkeypatch.setattr(attendance, "sync", lambda days: calls.append(("sync", days)) or {"method": "http"})
    monkeypatch.setattr(attendance_http, "keepalive", lambda: calls.append(("keepalive",)) or {})
    monkeypatch.setitem(M._attendance, "thread", None)
    monkeypatch.setattr(attendance_auto, "due", lambda records, last, now: ("morning", 3))
    assert M.start_attendance_work() == "sync"
    M._attendance["thread"].join(5)
    assert calls == [("sync", 3)] and attendance_auto.last_auto_run()
    monkeypatch.setattr(attendance_auto, "due", lambda records, last, now: None)
    assert M.start_attendance_work() is None
    monkeypatch.setattr(M, "load_settings", lambda: {"attendance_enabled": True, "attendance_keepalive_minutes": 30})
    monkeypatch.setitem(M._attendance, "last_keepalive", 0.0)
    assert M.start_attendance_work() == "keepalive"
    M._attendance["thread"].join(5)
    assert calls[-1] == ("keepalive",)
    monkeypatch.setattr(M, "load_settings", lambda: {"attendance_enabled": False})
    assert M.start_attendance_work() is None


def test_probe_reports_counts_only(monkeypatch, cache):
    fake_fetch(monkeypatch)

    def no_playwright():
        raise RuntimeError("not installed")
    monkeypatch.setattr(attendance, "_playwright", no_playwright)
    report = attendance_http.probe()
    text = json.dumps(report, ensure_ascii=False)
    assert report["http"]["days_offered"] == 3 and report["http"]["panels_in_html"] == 2
    assert report["http"]["rows_per_day"] == [3, 1]
    assert "FIT" not in text and "Entry.aspx" not in text and "Workshop" not in text
    assert "退回浏览器" in report["verdict"]


def test_keepalive_records_success_and_failure(monkeypatch, cache, tmp_path):
    state_file(monkeypatch, tmp_path, [])
    monkeypatch.setattr(attendance_http, "_get", lambda url: ("<html>ok</html>", ["n=1; Path=/"]))
    attendance_auto.record_login()
    assert attendance_http.keepalive() == {"authenticated": True, "renewed_cookies": 1}

    def expired(url):
        raise attendance.AttendanceAuthError("expired")
    monkeypatch.setattr(attendance_http, "_get", expired)
    with pytest.raises(attendance.AttendanceAuthError):
        attendance_http.keepalive()
    assert "expired_after_hours" in attendance_auto.lifetime()["current"]


def test_class_slots_use_the_portal_timezone():
    # 课表时间按门户的时区算，不按这台电脑的时区
    now = at(2, 10, 20)
    assert attendance_auto.due([record(2, "10:00AM")], (now - timedelta(minutes=10)).timestamp(), now) \
        == ("after_class", 1)


def test_certificate_problem_falls_back_instead_of_failing(monkeypatch, tmp_path, cache):
    import ssl
    state_file(monkeypatch, tmp_path, [])

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
    monkeypatch.setattr(attendance_http.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(attendance_http.FastPathUnavailable):
        attendance_http.fetch(attendance_http.UNITS)
    # sync 退回浏览器，probe 说明原因而不是报错退出
    from monash_study_kit import paths
    monkeypatch.setattr(paths, "load_settings", lambda: {"attendance_fast_read": True})
    monkeypatch.setattr(attendance, "_read_browser", lambda keys, script: {k: [] for k in keys})
    assert attendance.sync(1)["method"] == "browser"
    report = attendance_http.probe()
    assert report["http"]["ok"] is False and "证书" in report["verdict"]


def test_tls_uses_the_system_verifier_when_truststore_is_installed(monkeypatch):
    import ssl
    import sys
    import types
    made = []
    fake = types.ModuleType("truststore")
    fake.SSLContext = lambda protocol: made.append(protocol) or ssl.create_default_context()
    monkeypatch.setitem(sys.modules, "truststore", fake)
    context = attendance_http._tls()
    assert made == [ssl.PROTOCOL_TLS_CLIENT] and context.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setitem(sys.modules, "truststore", None)      # 没装时用 Python 默认的，校验照样开着
    assert attendance_http._tls().verify_mode == ssl.CERT_REQUIRED


def test_every_portal_request_counts_toward_the_login_lifetime(monkeypatch, cache):
    attendance_auto.record_login()
    data = json.loads(attendance_auto.SESSION_FILE.read_text())
    data["login_at"] = data["last_ok"] = data["login_at"] - 7200
    attendance_auto.SESSION_FILE.write_text(json.dumps(data))
    monkeypatch.setattr(attendance_http, "_get", lambda url: ("<html></html>", []))
    attendance_http.fetch(attendance_http.UNITS)
    assert attendance_auto.lifetime()["current"]["still_valid_after_hours"] >= 2.0

    def expired(url):
        raise attendance.AttendanceAuthError("expired")
    monkeypatch.setattr(attendance_http, "_get", expired)
    with pytest.raises(attendance.AttendanceAuthError):
        attendance_http.fetch(attendance_http.UNITS)
    assert "expired_after_hours" in attendance_auto.lifetime()["current"]


def test_real_portal_label_with_time_first_and_lowercase_pm():
    # 真实门户的写法：时间在前，“4:00 pm” 中间有空格、小写
    html = '<div id="p"><li>4:00 pm FIT2102 Workshop 01 <span class="glyphicon glyphicon-ok"></span></li></div>'
    (row,) = attendance_http.parse_day(htmldom.parse(html).find(id="p"), "20_Oct_26")
    assert (row["course"], row["session"], row["time"], row["completed"]) == ("FIT2102", "Workshop 01", "4:00 pm", True)
    now = at(3, 16, 20)
    assert attendance_auto.due([record(3, "4:00 pm")], now.timestamp() - 1800, now) == ("after_class", 1)
