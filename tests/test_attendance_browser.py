"""Real DOM fixtures for the reused extension parser; no university requests."""
import os

import pytest

from monash_study_kit import attendance


@pytest.fixture
def page():
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        pytest.skip("Set PLAYWRIGHT_BROWSERS_PATH to run the isolated DOM fixtures")
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_parser_keeps_same_day_classes_separate_and_real_entry_pending(page):
    page.set_content('''<div id="30_Oct_26"><ul>
      <li>FIT2102 Workshop 01 2:00PM <span class="unrelated-success">status</span><a href="https://attendance.monash.edu.my/student/Entry.aspx?id=1">Enter</a></li>
      <li>FIT2102 Workshop 02 4:00PM <span class="glyphicon-ok">✓</span></li>
      <li>FIT2109 Studio 01 2:00PM <span class="glyphicon-ok">✓</span></li>
    </ul></div>''')
    result = page.evaluate((attendance.ASSETS/"extract-rows.js").read_text(),"30_Oct_26")
    rows = {(r["course"],r["session"]):r for r in result["sessions"]}
    assert len(rows)==3
    assert rows["FIT2102","Workshop 01"]["completed"] is False
    assert rows["FIT2102","Workshop 02"]["completed"] is True
    assert rows["FIT2109","Studio 01"]["entryUrl"]==""


def test_matching_rejects_code_from_wrong_date_and_class(page):
    import re
    shared = (attendance.ASSETS/"shared.js").read_text()
    script = "({text,items}) => {"+re.sub(r"\bexport\s+","",shared)+";return matchCodesToAttendance(text,items);}"
    item = {"id":"a","course":"FIT2102","session":"Workshop 01","time":"2:00PM",
            "attendanceDate":{"iso":"2026-10-30","key":"30_Oct_26"}}
    result = page.evaluate(script,{"text":"FIT2102\nWorkshop 02 30 Oct 2026 2:00PM ABCDE", "items":[item]})
    assert result[0]["code"]==""
    result = page.evaluate(script,{"text":"FIT2102\nWorkshop 01 23 Oct 2026 2:00PM ABCDE", "items":[item]})
    assert result[0]["code"]==""
    result = page.evaluate(script,{"text":"FIT2102\nWorkshop 01 30 Oct 2026 2:00PM ABCDE", "items":[item]})
    assert result[0]["code"]=="ABCDE"


def test_open_day_uses_portal_day_menu_not_url_hash(page):
    # Mirrors the real portal: the hash is ignored, #daySel reveals div#dayPanel_<key>.
    html = '''<select id="daySel"><option value="30_Oct_26">Fri</option><option value="31_Oct_26" selected>Sat</option></select>
      <div id="dayPanel_30_Oct_26" style="display:none"><ul><li>FIT2102 Workshop 01 2:00PM</li></ul></div>
      <div id="dayPanel_31_Oct_26"><ul><li>FIT2109 Studio 01 9:00AM</li></ul></div>
      <script>document.getElementById("daySel").onchange = e => document.querySelectorAll("[id^=dayPanel_]")
        .forEach(p => p.style.display = p.id === "dayPanel_" + e.target.value ? "" : "none");</script>'''
    page.route(attendance.ORIGIN + "/student/Units.aspx", lambda route: route.fulfill(body=html, content_type="text/html"))
    assert attendance._open_day(page, "30_Oct_26") is True
    rows = page.evaluate((attendance.ASSETS/"extract-rows.js").read_text(), "30_Oct_26")["sessions"]
    assert [(r["course"], r["session"]) for r in rows] == [("FIT2102", "Workshop 01")]
    assert attendance._open_day(page, "1_Dec_26") is False
