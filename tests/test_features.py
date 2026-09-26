import time
"""页面解析的测试。HTML 片段是从 2026-09 的 Monash 页面里截下来再删短的。"""
from monash_study_kit import htmldom
from monash_study_kit import features as F
from monash_study_kit.htmldom import form_fields, parse


class FakeClient:
    def __init__(self, pages=None, calls=None):
        self.pages, self.calls, self.userid = pages or {}, calls or {}, 2000

    def html(self, url):
        for k, v in self.pages.items():
            if k in url:
                return v
        raise AssertionError(f"unexpected GET {url}")

    def call(self, method, args=None):
        return self.calls[method](args) if callable(self.calls[method]) else self.calls[method]

    def time_remaining(self):
        return 14000


GRADES = """<table class="generaltable boxaligncenter user-grade"><thead><tr><th>Grade item</th></tr></thead><tbody>
<tr class="cat_1"><th class="level2 item column-itemname" colspan="2"><div class="item"><div>
<span class="d-block text-uppercase small" title="Assignment">Assignment</span>
<div class="rowtitle"><a class="gradeitemheader" href="https://learning.monash.edu/mod/assign/view.php?id=6276145">Assignment 1</a></div></div></div></th>
<td class="level2 column-grade">17.00</td><td class="column-range">0&ndash;20</td><td class="column-percentage">85.00 %</td>
<td class="column-feedback"><p>Good work.</p><p>See <a href="https://learning.monash.edu/pluginfile.php/9/assignfeedback_file/feedback_files/1/marked.pdf">marked</a></p></td></tr>
<tr class="cat_1"><th class="column-itemname"><div class="rowtitle">Assignment 2</div></th>
<td class="column-grade">-</td><td class="column-range">0&ndash;15</td><td class="column-feedback">&nbsp;</td></tr>
</tbody></table>"""


def test_grades_table():
    rows = F.grades(FakeClient({"grade/report/user": GRADES}), 42804)
    assert rows[0]["item"] == "Assignment 1" and rows[0]["type"] == "Assignment"
    assert (rows[0]["grade"], rows[0]["range"], rows[0]["percentage"]) == ("17.00", "0–20", "85.00 %")
    assert rows[0]["feedback"] == "Good work.\nSee marked"
    assert rows[0]["feedback_files"][0].endswith("marked.pdf")
    assert rows[1]["grade"] == "-"
    assert len(F.grades(FakeClient({"grade/report/user": GRADES}), 1, graded_only=True)) == 1


FORUM = """<table><tbody>
<tr class="discussion" data-region="discussion-list-item" data-discussionid="795145" data-forumid="">
<td><a data-type="favorite-toggle" href="">star</a></td>
<th class="topic"><div><a class="w-100 h-100 d-block" href="https://learning.monash.edu/mod/forum/discuss.php?d=795145" title="In-class Test">
 In-class Test </a></div></th>
<td class="author-info"><div>Dr Example Lecturer</div><div><time datetime="2026-09-10T07:25:56+08:00">10 Sept</time></div></td>
<td><span data-region="replies">0</span></td></tr></tbody></table>"""


def test_discussion_list():
    d = F.discussions(FakeClient({"mod/forum/view.php": FORUM}), 5503281)
    assert d == [{"id": 795145, "title": "In-class Test",
                  "url": "https://learning.monash.edu/mod/forum/discuss.php?d=795145",
                  "author": "Dr Example Lecturer 10 Sept", "replies": "0",
                  "time": "2026-09-10T07:25:56+08:00", "pinned": False}]


SEARCH = """<article id="p1689530" class="forum-post-container mb-2" data-post-id="1689530" data-region="post">
<header><a href="https://learning.monash.edu/mod/forum/view.php?id=5503281">Announcements</a> -&gt;
<a href="https://learning.monash.edu/mod/forum/discuss.php?d=795145">In-class Test</a>
<h3 data-region-content="forum-post-core-subject">In-class Test</h3>
by <a href="https://learning.monash.edu/user/view.php?id=1000&amp;course=42804">Dr Example Lecturer</a>
- <time datetime="2026-09-10T07:25:56+08:00">Thursday</time></header>
<div id="post-content-1689530"><p>Dear students,</p><p>Test on week 9.</p></div>
<a href="https://learning.monash.edu/mod/forum/discuss.php?d=795145#p1689530">Permalink</a></article>"""


def test_forum_search_results():
    [p] = F.forum_search(FakeClient({"mod/forum/search.php": SEARCH}), "test", 42804)
    assert p["forum"] == "Announcements" and p["subject"] == "In-class Test"
    assert p["author"] == "Dr Example Lecturer" and p["text"] == "Dear students,\nTest on week 9."
    assert p["url"].endswith("#p1689530")


SOON = int(time.time()) + 3 * 86400


def test_due_events_split_monash_labels():
    ev = {"events": [
        {"name": "Assignment 2 - CALLISTA: FIT2109 MALAYSIA ON-CAMPUS S2 2026 (Due date)", "activityname": "Assignment 2",
         "modulename": "assign", "timesort": SOON + 100, "course": {"id": 42804, "fullname": "FIT2109 Computer science workshop - S2 2026"},
         "action": {"name": "Add submission", "actionable": True}, "url": "u1"},
        {"name": "In-Class Test (Using SEB) - Allocate+ FIT2109 Workshop 02 closes", "activityname": "In-Class Test (Using SEB)",
         "modulename": "quiz", "timesort": SOON, "course": {"id": 42804, "fullname": "FIT2109 x - S2 2026"},
         "action": {"name": "Attempt quiz now"}, "url": "u2"},
    ]}
    items = F.due(FakeClient(calls={"core_calendar_get_action_events_by_timesort": ev}), 14)
    assert [i["activity"] for i in items] == ["In-Class Test (Using SEB)", "Assignment 2"]
    assert [i["kind"] for i in items] == ["closes", "due date"]
    assert items[0]["course"] == "FIT2109" and not items[0]["overdue"]


def test_find_matches_numbers_as_whole_words():
    assert F._score("week 7", "Week 7 - Haskell") > 0
    assert F._score("week 7", "Week 17 - Review") == 0
    assert F._score("applied solutions", "Applied 5 Exercises Sample Solutions") > 0
    assert F._score("slides", "Week 1 Tutorial") == 0


def test_find_course_prefers_latest_offering():
    cs = [{"id": 1, "fullname": "FIT2102 Programming paradigms - S2 2025", "startdate": 100},
          {"id": 2, "fullname": "FIT2102 Programming paradigms - S2 2026", "startdate": 200}]
    assert F.find_course(cs, "fit2102")["id"] == 2
    assert F.find_course(cs, "1")["id"] == 1
    assert F.find_course(cs, "https://learning.monash.edu/course/view.php?id=1")["id"] == 1


def test_form_fields_like_a_browser():
    form = parse("""<form><input type="hidden" name="sesskey" value="k">
      <input type="checkbox" name="a" value="1" checked><input type="checkbox" name="b" value="1">
      <input type="radio" name="q1" value="0"><input type="radio" name="q1" value="2" checked>
      <select name="s"><option value="x">X</option><option value="y" selected>Y</option></select>
      <textarea name="t">hello</textarea><input type="submit" name="go" value="Go"></form>""").find("form")
    assert form_fields(form) == [("sesskey", "k"), ("a", "1"), ("q1", "2"), ("s", "y"), ("t", "hello")]


def test_dom_autoclose_and_text():
    doc = htmldom.parse("<ul><li>one<li>two</ul><p>a<p>b")
    assert [li.text() for li in doc.find_all("li")] == ["one", "two"]
    assert [p.text() for p in doc.find_all("p")] == ["a", "b"]


UPCOMING = {"events": [
    {"id": 2, "name": "In-Class Test (Using SEB) opens", "activityname": "In-Class Test (Using SEB)", "modulename": "quiz",
     "eventtype": "open", "timestart": SOON - 50, "course": {"id": 42804, "fullname": "FIT2109 x - S2 2026"}},
    {"id": 1, "name": "Assignment 2 (Due date)", "eventtype": "due", "timestart": SOON,
     "course": {"id": 42804, "fullname": "FIT2109 x - S2 2026"}},
    {"id": 3, "name": "Far away", "eventtype": "course", "timestart": SOON + 60 * 86400, "course": {"id": 1}},
]}


def test_due_merges_upcoming_view_without_duplicates():
    action = {"events": [{"id": 1, "name": "Assignment 2 (Due date)", "timesort": SOON, "modulename": "assign",
                          "course": {"id": 42804, "fullname": "FIT2109 x - S2 2026"}, "action": {"name": "Add submission"}}]}
    items = F.due(FakeClient(calls={"core_calendar_get_action_events_by_timesort": action,
                                    "core_calendar_get_calendar_upcoming_view": UPCOMING}), 14)
    assert [(i["id"], i["kind"]) for i in items] == [(2, "opens"), (1, "due date")]
    assert items[1]["action"] == "Add submission"      # 同一事件保留 action events 那份


ICS = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:2749891@learning.monash.edu\r\n"
       "SUMMARY:Week 8 Applied Session Submission - Allocate+ FIT2102 Tutorial 09 M\r\n\tA Wed 14:00 (Due date)\r\n"
       "DESCRIPTION:Submit a zip\\, please\\nthanks\r\nDTSTART:{start}\r\nCATEGORIES:FIT2102_S2_2026\r\n"
       "END:VEVENT\r\nEND:VCALENDAR\r\n")


def test_parse_ical_unfolds_and_unescapes():
    ev = F.parse_ical(ICS.format(start="20260930T055500Z"))
    assert ev[0]["SUMMARY"].endswith("Tutorial 09 MA Wed 14:00 (Due date)")
    assert ev[0]["DESCRIPTION"] == "Submit a zip, please\nthanks"
    it = F.ical_items(ICS.format(start="20260930T055500Z"), now=0)[0]
    assert (it["id"], it["course"], it["kind"], it["due_ts"]) == (2749891, "FIT2102", "due date", 1790747700)


def test_due_falls_back_to_ical_when_session_is_dead(monkeypatch):
    from monash_study_kit.moodlelib import MoodleAuthError

    def dead(_):
        raise MoodleAuthError("expired")
    start = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(SOON))
    monkeypatch.setattr(F, "load_calendar_url", lambda: "https://x/calendar/export_execute.php?authtoken=t")
    monkeypatch.setattr(F, "fetch_ical", lambda url: ICS.format(start=start))
    items = F.due(FakeClient(calls={"core_calendar_get_action_events_by_timesort": dead}), 14)
    assert [i["source"] for i in items] == ["ical"]
    monkeypatch.setattr(F, "load_calendar_url", lambda: None)
    import pytest
    with pytest.raises(MoodleAuthError):
        F.due(FakeClient(calls={"core_calendar_get_action_events_by_timesort": dead}), 14)


def test_mask_calendar_url():
    assert F.mask_calendar_url("https://x/e.php?userid=1&authtoken=abc123&p=1") == "https://x/e.php?userid=1&authtoken=…&p=1"


ASSIGN_INDEX = """<table class="generaltable"><thead><tr><th>Section</th><th>Assignments</th><th>Due date</th>
<th>Submission</th><th>Grade</th></tr></thead><tbody>
<tr><td class="cell c0">1. Project</td><td class="cell c1"><a href="https://learning.monash.edu/mod/assign/view.php?id=11">A1</a></td>
<td class="cell c2">Sunday, 6 September 2026, 11:55 PM</td><td class="cell c3">Submitted for grading</td><td class="cell c4">12.94</td></tr>
<td colspan="5"><div class="tabledivider"></div></td></tr>
<tr><td class="cell c0">3. Quiz / Test</td><td class="cell c1"><a href="https://learning.monash.edu/mod/assign/view.php?id=12">Quiz 1</a></td>
<td class="cell c2">-</td><td class="cell c3">No submission</td><td class="cell c4">5.00</td></tr>
<tr><td class="cell c0"></td><td class="cell c1"><a href="https://learning.monash.edu/mod/assign/view.php?id=13">Interview</a></td>
<td class="cell c2">Thursday, 17 September 2026, 9:55 PM</td><td class="cell c3">No submission</td><td class="cell c4">-</td></tr>
<tr><td class="cell c0"></td><td class="cell c1"><a href="https://learning.monash.edu/mod/assign/view.php?id=14">Later</a></td>
<td class="cell c2">Monday, 5 October 2026, 11:55 PM</td><td class="cell c3">No submission</td><td class="cell c4">-</td></tr>
</tbody></table>"""


def test_parse_page_time_uses_account_timezone():
    assert F.parse_page_time("Sunday, 6 September 2026, 11:55 PM") == 1788710100   # 2026-09-06 15:55Z
    assert F.parse_page_time("-") is None


def test_assignments_flags_only_past_unsubmitted_ungraded():
    course = {"id": 42804, "fullname": "FIT2109 x - S2 2026"}
    items = F.assignments(FakeClient(pages={"/mod/assign/index.php": ASSIGN_INDEX}), course, now=1790000000)
    assert [(i["name"], i["section"], i["missing"]) for i in items] == [
        ("A1", "1. Project", False), ("Quiz 1", "3. Quiz / Test", False),
        ("Interview", "3. Quiz / Test", True), ("Later", "3. Quiz / Test", False)]
    assert items[0]["cmid"] == 11 and items[0]["grade"] == "12.94" and items[1]["due"] is None


def test_conversations_and_messages():
    me = {"id": 2000, "fullname": "Student Me"}
    other = {"id": 7, "fullname": "Student Friend"}
    calls = {
        "core_session_time_remaining": {"userid": 2000, "timeremaining": 100},
        "core_message_get_conversations": {"conversations": [
            {"id": 1, "type": 1, "name": "", "membercount": 2, "unreadcount": 2, "isread": False,
             "members": [other], "messages": [{"useridfrom": 7, "text": "<p>hi <b>there</b></p>", "timecreated": 1767884788}]},
            {"id": 2, "type": 3, "name": "", "membercount": 1, "unreadcount": None, "isread": True,
             "members": [me], "messages": []}]},
        "core_message_get_conversation_messages": {"members": [me, other], "messages": [
            {"useridfrom": 2000, "text": "<p>second</p>", "timecreated": 20},
            {"useridfrom": 7, "text": "<p>first</p>", "timecreated": 10}]},
    }
    client = FakeClient(calls=calls)
    client.time_remaining = lambda: setattr(client, "userid", 2000)
    convs = F.conversations(client)
    assert [(c["type"], c["name"], c["unread"]) for c in convs] == [("private", "Student Friend", 2), ("self", "（自己）", 0)]
    assert convs[0]["last"]["from"] == "Student Friend" and convs[0]["last"]["text"] == "hi there"
    msgs = F.conversation_messages(client, 1)["messages"]
    assert [(m["from"], m["text"]) for m in msgs] == [("Student Friend", "first"), ("我", "second")]
