"""同一个页面，浏览器里跑原来的 extract-rows.js 和 Python 版 parse_day，结果要一样。

要真浏览器，和 test_attendance_browser.py 一样没设 PLAYWRIGHT_BROWSERS_PATH 或没装 playwright 就跳过。
"""
import os

import pytest

attendance = pytest.importorskip("monash_study_kit.attendance")
from monash_study_kit import attendance_http, htmldom  # noqa: E402

ENTRY = "https://attendance.monash.edu.my/student/Entry.aspx"

PANELS = {
    "list with entry links and check icons": f'''<ul>
      <li>FIT2102 Workshop 01 2:00PM <span class="unrelated-success">status</span><a href="{ENTRY}?id=1">Enter</a></li>
      <li>FIT2102 Workshop 02 4:00PM <span class="glyphicon-ok">✓</span></li>
      <li>FIT2109 Studio 01 2:00PM <span class="glyphicon glyphicon-ok"></span></li></ul>''',
    "table rows split across cells": f'''<table>
      <tr><td>FIT2109</td><td>Applied Class 2</td><td>9:00 AM</td><td><a href="{ENTRY}?id=7">Enter code</a></td></tr>
      <tr><td>FIT2014</td><td>Lab 05</td><td>11:00 am</td><td><i class="fa fa-check"></i></td></tr></table>''',
    "nested divs and icon images": f'''<div class="activity-card"><div><b>FIT3171</b> Tutorial 3</div>
        <div>1:00PM - 3:00PM</div><img src="/img/tick.png" alt="present"></div>
      <div class="activity-card"><div><b>FIT3171</b> Seminar</div><div>5:00PM</div>
        <a href="{ENTRY}?id=9">Enter</a></div>''',
    "hidden rows and pending wording": f'''<ul>
      <li style="display:none">FIT9999 Tutorial 03 9:00AM <span class="glyphicon-ok"></span></li>
      <li>FIT2102 Practical 2 3:00PM <span title="question">?</span> pending</li>
      <li>FIT2102 Laboratory 07 5:00PM <a href="{ENTRY}?mah_completed=1">done</a></li></ul>''',
}


@pytest.fixture(scope="module")
def page():
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        pytest.skip("Set PLAYWRIGHT_BROWSERS_PATH to run the browser comparison")
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser.new_context().new_page()
        browser.close()


@pytest.mark.parametrize("name", PANELS)
def test_python_parser_matches_the_extension_parser(page, name):
    key = "30_Oct_26"
    # 页面上有一个隐藏的别的日期，确认两边都只读选中的那天
    html = (f'<div id="dayPanel_29_Oct_26" style="display:none"><li>FIT1111 Workshop 09 8:00AM</li></div>'
            f'<div id="dayPanel_{key}">{PANELS[name]}</div>')
    page.set_content(html)
    js = page.evaluate((attendance.ASSETS / "extract-rows.js").read_text(), key)["sessions"]
    panel = htmldom.parse(html).find(id="dayPanel_" + key)
    py = attendance_http.parse_day(panel, key, page.url)
    strip = lambda rows: sorted(({k: v for k, v in r.items() if k != "sourceUrl"} for r in rows),
                                key=lambda r: (r["course"], r["session"]))
    assert js, "fixture should produce rows"
    assert strip(py) == strip(js)


def test_browser_read_loads_the_page_once_and_switches_days(page, monkeypatch):
    from contextlib import contextmanager
    html = '''<select id="daySel"><option value="30_Oct_26">Fri</option><option value="31_Oct_26" selected>Sat</option></select>
      <div id="dayPanel_30_Oct_26" style="display:none"><ul><li>FIT2102 Workshop 01 2:00PM</li></ul></div>
      <div id="dayPanel_31_Oct_26"><ul><li>FIT2109 Studio 01 9:00AM</li><li>FIT2109 Studio 02 11:00AM</li></ul></div>
      <script>document.getElementById("daySel").onchange = e => document.querySelectorAll("[id^=dayPanel_]")
        .forEach(p => p.style.display = p.id === "dayPanel_" + e.target.value ? "" : "none");</script>'''
    loads = []
    context = page.context

    def serve(route):
        loads.append(route.request.url)
        route.fulfill(body=html, content_type="text/html")
    context.route(attendance.ORIGIN + "/student/Units.aspx", serve)

    @contextmanager
    def fake_browser():
        yield context
    monkeypatch.setattr(attendance, "browser", fake_browser)
    monkeypatch.setattr(attendance, "_save_state", lambda c: None)
    script = (attendance.ASSETS / "extract-rows.js").read_text()
    out = attendance._read_browser(["30_Oct_26", "31_Oct_26", "1_Nov_26"], script)
    context.unroute(attendance.ORIGIN + "/student/Units.aspx")
    assert len(loads) == 1
    assert [r["session"] for r in out["30_Oct_26"]] == ["Workshop 01"]
    assert len(out["31_Oct_26"]) == 2 and out["1_Nov_26"] is None
