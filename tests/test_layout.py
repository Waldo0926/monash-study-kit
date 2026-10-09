"""目录映射和各种解析的测试。章节结构照抄 2026-09 FIT2102 的 core_courseformat_get_state。"""
from monash_study_kit.moodlelib import collect_links, is_attachment, parse_cookie_input
from monash_study_kit.syncer import _same_file, course_code, course_folder, is_external, plan_layout, safe_name, week_folder


def sec(number, title, parent=None, cmlist=()):
    return {"number": number, "title": title, "parent": parent, "cmlist": list(cmlist),
            "sectionurl": f"https://learning.monash.edu/course/view.php?id=1&section={number}"}


STATE = {"section": [
    sec(0, "Unit dashboard", None, ["1"]),
    sec(3, "Learning", None),
    sec(4, "Getting started", 3, ["2"]),
    sec(7, "Week 1 - Introduction to Functional Programming in JavaScript", 3, ["3"]),
    sec(8, "Own-time", 7, ["4"]),
    sec(9, "Real-time", 7, ["5"]),
    sec(56, "Assessments", 0, ["6"]),
    sec(58, "2. Written", 56, ["7"]),
    sec(60, "Week 12 &amp; Review", 3, []),
]}


def test_week_sections_and_children_share_one_folder():
    folders = plan_layout(STATE).folders
    week = "Week 01 - Introduction to Functional Programming in JavaScript"
    assert folders[7] == folders[8] == folders[9] == week
    assert folders[60] == "Week 12 - & Review"


def test_empty_container_sections_are_dropped_from_the_path():
    folders = plan_layout(STATE).folders
    assert folders[4] == "Getting started"          # 不是 Learning/Getting started
    assert folders[58] == "Assessments/2. Written"  # Assessments 自己有内容，保留；第 0 节不算
    assert folders[0] == "Unit dashboard"


def test_course_folder_names():
    c = {"id": 1, "fullname": "FIT2102 Programming paradigms - S2 2026", "shortname": "FIT2102_S2_2026"}
    assert course_folder(c) == "FIT2102 Programming paradigms (S2 2026)"
    assert course_code(c) == "FIT2102"
    c = {"id": 2, "fullname": "FIT1043\tIntroduction to data science - MUM S2 2025"}
    assert course_folder(c) == "FIT1043 Introduction to data science (MUM S2 2025)"
    c = {"id": 3, "fullname": "FIT3161 - FIT3163 COMPUTER &amp; DATA SCIENCE PROJECT 1 - MUM S1 2026"}
    assert course_folder(c) == "FIT3161 - FIT3163 COMPUTER & DATA SCIENCE PROJECT 1 (MUM S1 2026)"
    c = {"id": 4, "fullname": "FIT3199 Industry Work Experience - Summer MUM 2026"}
    assert course_folder(c) == "FIT3199 Industry Work Experience (Summer MUM 2026)"
    assert course_code({"id": 5, "fullname": "IT Student Portal", "shortname": "FIT-Student-Portal"}) is None


def test_week_folder_pads_numbers():
    assert week_folder("Week 3 - Editor/Modern IDE") == "Week 03 - Editor Modern IDE"
    assert week_folder("Week 10") == "Week 10"
    assert week_folder("Weekly quiz") is None


def test_safe_name_keeps_extension_when_truncating():
    name = safe_name("a" * 200 + ".pdf", limit=50)
    assert len(name) <= 50 and name.endswith(".pdf")
    assert safe_name('a/b:c?"d') == "a b c d"


def test_parse_cookie_input_variants():
    curl = ("curl 'https://learning.monash.edu/my/' \\\n  -H 'accept: text/html' \\\n"
            "  -b 'MoodleSession=abc123; MOODLEID1_=xyz; _ga=1' \\\n  -H 'user-agent: x'")
    assert parse_cookie_input(curl)["MoodleSession"] == "abc123"
    curl_h = "curl 'https://x' -H 'Cookie: MoodleSession=q1; AWSALB=z'"
    assert parse_cookie_input(curl_h) == {"MoodleSession": "q1", "AWSALB": "z"}
    assert parse_cookie_input("MoodleSession=v; other=1")["MoodleSession"] == "v"
    assert parse_cookie_input("  justthevalue \n") == {"MoodleSession": "justthevalue"}
    assert parse_cookie_input("") == {}


def test_attachment_filter():
    base = "https://learning.monash.edu/pluginfile.php"
    assert is_attachment(f"{base}/7319404/mod_assign/introattachment/0/A1%20spec.pdf")
    assert is_attachment(f"{base}/1/mod_label/intro/Week%201%20slides.pptx?forcedownload=1")
    assert not is_attachment(f"{base}/7319404/theme_monash/banner/0/paradigms.png")
    assert not is_attachment(f"{base}/1/msttools_imglib/msttools_imglib_files/0/learning-tab/owntime-bg.svg")
    assert not is_attachment(f"{base}/7319436/mod_url/intro/ForkButton.PNG")
    assert not is_attachment("https://example.com/a.pdf")


def test_external_links():
    assert is_external("https://tgdwyer.github.io/functionaljavascript")
    assert not is_external("https://learning.monash.edu/mod/assign/view.php?id=1")
    assert not is_external("mailto:someone@monash.edu")
    assert not is_external("#section-3")


def test_same_file_ignores_revision():
    a = "https://learning.monash.edu/pluginfile.php/9/mod_resource/content/3/applied1.zip"
    b = "https://learning.monash.edu/pluginfile.php/9/mod_resource/content/4/applied1.zip"
    c = "https://learning.monash.edu/pluginfile.php/9/mod_resource/content/4/applied2.zip"
    assert _same_file(a, b) and not _same_file(a, c)


def test_link_collector_groups_by_activity():
    page = """
    <ul><li class="activity activity-wrapper label modtype_label" data-id="5761604">
      <div><p>Read <a href="https://tgdwyer.github.io/javascript1">JS <b>intro</b></a><br>
      <img src="x.png"></p></div></li>
    <li class="activity resource modtype_resource" data-id="6308413">
      <a href="https://learning.monash.edu/mod/resource/view.php?id=6308413">Applied 1</a>
      <a href="https://learning.monash.edu/pluginfile.php/1/mod_resource/intro/extra.pdf">extra</a></li></ul>
    <a href="https://outside.example">footer</a>"""
    found = collect_links(page)
    assert found.by_activity["5761604"] == [("https://tgdwyer.github.io/javascript1", "JS intro")]
    assert [h for h, _ in found.by_activity["6308413"]][1].endswith("extra.pdf")
    assert ("https://outside.example", "footer") in found.all
    assert all(h != "https://outside.example" for acts in found.by_activity.values() for h, _ in acts)


def test_login_redirect_is_detected_before_following():
    import pytest
    from monash_study_kit.moodlelib import MoodleAuthError, _check_login_url
    with pytest.raises(MoodleAuthError):
        _check_login_url("https://monashuni.okta.com/app/x/sso/saml?SAMLRequest=1")
    with pytest.raises(MoodleAuthError):
        _check_login_url("https://learning.monash.edu/login/index.php")
    _check_login_url("https://d25zr1xy094zys.cloudfront.net/10/83/abc")  # 不抛
