"""只读功能：截止日期、成绩、公告、论坛、通知、查找、单个下载。

每个函数返回普通的 dict / list，CLI、网页和 MCP 共用同一份逻辑。

哪些走接口、哪些解析页面，是在 Monash 上逐个试出来的（2026-09）：
  接口能用：core_calendar_get_action_events_by_timesort、core_calendar_get_calendar_upcoming_view、
           mod_forum_get_discussion_posts、
           message_popup_get_popup_notifications、core_message_get_unread_conversation_counts
  没开放（"Web service is not available"）：gradereport_user_*、mod_forum_get_forum_discussions、
           mod_assign_get_submission_status、core_search_get_results：这些都解析页面
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import htmldom, moodlelib
from .moodlelib import BASE_URL, HOST, MoodleAuthError, MoodleClient, filename_from, is_attachment
from .paths import SECRETS_DIR, TZ_OFFSET, write_secret
from .syncer import clean, course_code, safe_name

MOODLE_URL_RE = re.compile(r"/mod/(\w+)/view\.php\?(?:.*&)?id=(\d+)")


# ---------------------------------------------------------------- 课程引用

def find_course(courses: list[dict], ref: str) -> dict:
    """课号 / id / 课程 URL / 名字片段 → 课程。同课号多期取最新。"""
    ref = ref.strip()
    m = re.search(r"course/view\.php\?id=(\d+)", ref)
    if m:
        ref = m.group(1)
    by_start = sorted(courses, key=lambda c: -(c.get("startdate") or 0))
    hits = [c for c in by_start if str(c["id"]) == ref] \
        or [c for c in by_start if (course_code(c) or "") == ref.upper()] \
        or [c for c in by_start if ref.lower() in clean(c.get("fullname", "")).lower()]
    if not hits:
        raise LookupError(f"找不到课程 {ref}")
    return hits[0]


def _ts(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes") if ts else None


# ---------------------------------------------------------------- 截止日期

KIND_RE = re.compile(r"\((Due date|Cut-off date|Opens|Closes)\)\s*$|\b(opens|closes|is due)\s*$", re.I)
# upcoming view 的 eventtype → 和 action events 名字后缀一致的 kind
EVENTTYPE_KIND = {"due": "due", "open": "opens", "close": "closes", "gradingdue": "grading due",
                  "expectcompletionon": "expected"}


def _due_item(e: dict, now: int) -> dict:
    c = e.get("course") or {}
    action = e.get("action") or {}
    name = clean(e.get("name", ""))
    kind = KIND_RE.search(name)
    ts = e.get("timesort") or e.get("timestart")
    return {
        "id": e.get("id"),
        "course": course_code(c) or clean(c.get("shortname")),
        "course_id": c.get("id"),
        "name": re.sub(r"\s+is due$", "", name),
        "kind": (kind.group(1) or kind.group(2)).lower() if kind
                else EVENTTYPE_KIND.get(e.get("eventtype"), e.get("eventtype") or "event"),
        "activity": clean(e.get("activityname")),
        "module": e.get("modulename"),
        "due": _ts(ts),
        "due_ts": ts,
        "overdue": bool(e.get("overdue")) or (ts or 0) < now,
        "action": action.get("name"),
        "actionable": bool(action.get("actionable")),
        "url": e.get("url") or e.get("viewurl"),
        "source": "api",
    }


def due(client: MoodleClient | None, days: int = 14, course: dict | None = None,
        include_overdue_days: int = 7) -> list[dict]:
    """截止日期和日历事件。

    action events 只含“要你做点什么”的事件（交作业、测验关闭），测验开放、课程/个人日历事件
    不在里面，所以再并上 upcoming view（Moodle 首页“即将发生”那块，范围是站点默认的几周）。
    会话失效时退回 iCal 订阅链接（不需要会话，见 calendar_url）；没存链接就照常抛 MoodleAuthError。
    """
    now = int(time.time())
    lo, hi = now - include_overdue_days * 86400, now + days * 86400
    try:
        if client is None:
            raise MoodleAuthError("还没登录 Moodle")
        events = client.call("core_calendar_get_action_events_by_timesort",
                             {"timesortfrom": lo, "timesortto": hi, "limitnum": 50})["events"]
        try:
            events += client.call("core_calendar_get_calendar_upcoming_view",
                                  {"courseid": 1, "categoryid": 0})["events"]
        except MoodleAuthError:
            raise
        except Exception:
            pass   # 补充来源，拿不到就算了
        items = [_due_item(e, now) for e in events]
    except MoodleAuthError:
        url = load_calendar_url()
        if not url:
            raise
        items = ical_items(fetch_ical(url), now)
    out, seen = [], set()
    for i in items:
        key = i["id"] if i["id"] is not None else (i["name"], i["due_ts"])
        if key in seen or not (lo <= (i["due_ts"] or 0) <= hi):
            continue
        if course and not ((i["course_id"] is not None and i["course_id"] == course["id"])
                           or i["course"] == course_code(course)):
            continue
        seen.add(key)
        out.append(i)
    return sorted(out, key=lambda x: x["due_ts"] or 0)


# ---------------------------------------------------------------- 日历订阅（iCal）
#
# calendar/export.php 的 “Get calendar URL” 给一个带 authtoken 的 export_execute.php 链接。
# 这个链接不需要登录会话，会话过期了照样能拿到截止日期，也能直接订阅进苹果/Google 日历。
# authtoken 由用户 id 和账号密码的盐算出来，等同于“只读日历密码”：存 600 权限，别打印全文。

CALENDAR_URL_FILE = SECRETS_DIR / "calendar-url"


def load_calendar_url() -> str | None:
    try:
        url = CALENDAR_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return url or None


def save_calendar_url(url: str) -> None:
    write_secret(CALENDAR_URL_FILE, url + "\n")


def mask_calendar_url(url: str) -> str:
    return re.sub(r"(authtoken=)\w+", r"\1…", url)


def calendar_url(client: MoodleClient, refresh: bool = False) -> str:
    """拿（必要时生成并保存）iCal 订阅链接：所有课程、最近 + 接下来的事件。"""
    if not refresh and (url := load_calendar_url()):
        return url
    r = client.post("/calendar/export.php", [
        ("sesskey", client.sesskey()), ("_qf__core_calendar_export_form", "1"),
        ("events[exportevents]", "all"), ("period[timeperiod]", "recentupcoming"),
        ("generateurl", "Get calendar URL")])
    m = re.search(r"https?://[^\s\"'<>]+/calendar/export_execute\.php\?[^\s\"'<>]+",
                  html.unescape(r.text()))
    if not m:
        raise RuntimeError("导出页没有给出日历链接（页面结构变了？）")
    save_calendar_url(m.group(0))
    return m.group(0)


def fetch_ical(url: str, timeout: int = 30) -> str:
    """不带 cookie 取 iCal。"""
    req = urllib.request.Request(url, headers={"User-Agent": moodlelib.UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_ical(text: str) -> list[dict]:
    """最小的 VEVENT 解析：折行、转义、UTC 时间。只取我们用得到的字段。"""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            key = key.split(";", 1)[0].upper()
            cur[key] = re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), val)
    return events


def _ical_ts(v: str | None) -> int | None:
    if not v:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return int(datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def ical_items(text: str, now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    out = []
    for e in parse_ical(text):
        name = clean(e.get("SUMMARY", ""))
        kind = KIND_RE.search(name)
        ts = _ical_ts(e.get("DTSTART"))
        cat = e.get("CATEGORIES", "")
        out.append({
            "id": int(e["UID"].split("@")[0]) if e.get("UID", "").split("@")[0].isdigit() else e.get("UID"),
            "course": (m.group(0) if (m := re.search(r"[A-Z]{3}\d{4}", cat)) else cat) or None,
            "course_id": None,
            "name": re.sub(r"\s+is due$", "", name),
            "kind": (kind.group(1) or kind.group(2)).lower() if kind else "event",
            "activity": None, "module": None,
            "due": _ts(ts), "due_ts": ts, "overdue": (ts or 0) < now,
            "action": None, "actionable": False, "url": None,
            "source": "ical",
        })
    return out


# ---------------------------------------------------------------- 成绩

def grades_overview(client: MoodleClient) -> list[dict]:
    doc = htmldom.parse(client.html("/grade/report/overview/index.php"))
    out = []
    for table in doc.find_all("table", id=lambda v: v.startswith("overview-grade")):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                a = tds[0].find("a")
                cid = re.search(r"[?&]id=(\d+)", a.get("href", "")) if a else None
                out.append({"course": tds[0].text(), "course_id": int(cid.group(1)) if cid else None,
                            "grade": tds[1].text()})
    return out


def grades(client: MoodleClient, course_id: int, graded_only: bool = False) -> list[dict]:
    doc = htmldom.parse(client.html(f"/grade/report/user/index.php?id={course_id}"))
    table = doc.find("table", cls="user-grade")
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        name_cell = tr.find("th", cls="column-itemname")
        grade = tr.find("td", cls="column-grade")
        if not (name_cell and grade):
            continue
        title = name_cell.find(cls="rowtitle") or name_cell
        kind = name_cell.find("span", cls="text-uppercase")
        link = title.find("a")
        cell = lambda c: (tr.find("td", cls=c).text() if tr.find("td", cls=c) else "")  # noqa: E731
        fb = tr.find("td", cls="column-feedback")
        item = {
            "item": title.text(),
            "type": kind.text() if kind else "",
            "grade": grade.text(),
            "range": cell("column-range"),
            "percentage": cell("column-percentage"),
            "weight": cell("column-weight"),
            "feedback": fb.text(sep="\n") if fb else "",
            "feedback_files": [a.get("href") for a in (fb.find_all("a") if fb else []) if is_attachment(a.get("href", ""))],
            "url": link.get("href") if link else None,
        }
        if graded_only and item["grade"] in ("", "-"):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------- 作业汇总

# 页面上的时间按 Moodle 账号的时区显示。马来西亚校区的账号是 UTC+8（和日历接口对得上：11:55 PM ↔ 15:55Z）；
# 澳洲校区的同学 `monash config tz_offset 10`（夏令时 11）。
PAGE_TZ = timezone(timedelta(hours=TZ_OFFSET))
NOT_SUBMITTED = {"no submission", "not submitted", "draft (not submitted)", "no attempt", "new"}


def parse_page_time(text: str) -> int | None:
    """"Sunday, 6 September 2026, 11:55 PM" → 时间戳；"-" 或认不出来 → None。"""
    text = clean(text)
    for fmt in ("%A, %d %B %Y, %I:%M %p", "%d %B %Y, %I:%M %p"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=PAGE_TZ).timestamp())
        except ValueError:
            continue
    return None


def assignments(client: MoodleClient, course: dict, now: int | None = None) -> list[dict]:
    """/mod/assign/index.php：一门课所有作业的截止时间、提交状态、成绩，一页拿全。

    missing = 已过截止、没交、也没分。只是“可能漏交”：面试、现场展示这类作业本来就不用在线交，
    Monash 也常把线下测验挂成作业（No submission 但有分，那种不算）。
    """
    now = now or int(time.time())
    doc = htmldom.parse(client.html(f"/mod/assign/index.php?id={course['id']}"))
    table = doc.find("table", cls="generaltable")
    if table is None:
        return []
    out, section = [], ""
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        section = tds[0].text() or section
        link = tds[1].find("a")
        due_ts = parse_page_time(tds[2].text())
        status, grade = tds[3].text(), tds[4].text()
        graded = grade not in ("", "-")
        submitted = status.lower() not in NOT_SUBMITTED
        m = re.search(r"[?&]id=(\d+)", link.get("href", "")) if link else None
        out.append({
            "course": course_code(course) or clean(course.get("shortname")),
            "course_id": course["id"],
            "section": section,
            "name": tds[1].text(),
            "cmid": int(m.group(1)) if m else None,
            "due": _ts(due_ts), "due_ts": due_ts,
            "status": status, "submitted": submitted,
            "grade": grade if graded else None,
            "missing": bool(due_ts and due_ts < now and not submitted and not graded),
            "url": link.get("href") if link else None,
        })
    return out


# ---------------------------------------------------------------- 论坛 / 公告

def forums(client: MoodleClient, course_id: int, state: dict | None = None) -> list[dict]:
    state = state or client.course_state(course_id)
    return [{"id": int(cm["id"]), "name": clean(cm["name"]), "url": cm.get("url"),
             "section": cm.get("sectionnumber")}
            for cm in state["cm"] if cm.get("module") == "forum" and cm.get("uservisible", True)]


def discussions(client: MoodleClient, forum_cmid: int, limit: int = 20) -> list[dict]:
    doc = htmldom.parse(client.html(f"/mod/forum/view.php?id={forum_cmid}"))
    out = []
    for tr in doc.find_all("tr", data__region="discussion-list-item"):
        a = tr.find("a", cls="w-100") or tr.find("a", href=lambda h: "discuss.php?d=" in h)
        if not a:
            continue
        author = tr.find(cls="author-info") or (tr.find_all("td")[2] if len(tr.find_all("td")) > 2 else None)
        replies = tr.find(cls="replies") or tr.find(data__region="replies")
        times = [t.get("datetime") for t in tr.find_all("time") if t.get("datetime")]
        out.append({"id": int(tr.get("data-discussionid")), "title": a.text(), "url": a.get("href"),
                    "author": author.text() if author else "", "replies": replies.text() if replies else None,
                    "time": times[0] if times else None,
                    "pinned": "pinned" in tr.classes or tr.find(data__region="pinned-label", hidden=None) is not None})
        if len(out) >= limit:
            break
    return out


def discussion_posts(client: MoodleClient, discussion_id: int) -> list[dict]:
    data = client.call("mod_forum_get_discussion_posts",
                       {"discussionid": discussion_id, "sortby": "created", "sortdirection": "ASC"})
    return [{"id": p["id"], "subject": clean(p["subject"]), "author": clean((p.get("author") or {}).get("fullname")),
             "time": _ts(p.get("timecreated")), "time_ts": p.get("timecreated"),
             "text": htmldom.parse(p.get("message") or "").text(sep="\n"),
             "parent": p.get("parentid"),
             "attachments": [{"name": a.get("filename"), "url": a.get("url")} for a in p.get("attachments") or []],
             "url": f"{BASE_URL}/mod/forum/discuss.php?d={discussion_id}#p{p['id']}"}
            for p in data.get("posts", [])]


def news(client: MoodleClient, course: dict, limit: int = 5, state: dict | None = None) -> list[dict]:
    """课程公告：第 0 节里叫 Announcements 的论坛，每帖带上首帖正文。"""
    fs = forums(client, course["id"], state)
    ann = [f for f in fs if "announcement" in f["name"].lower()] or [f for f in fs if f["section"] == 0]
    if not ann:
        return []
    out = []
    for d in discussions(client, ann[0]["id"], limit):
        posts = discussion_posts(client, d["id"])
        first = posts[0] if posts else {}
        out.append({**d, "course": course_code(course) or clean(course.get("shortname")),
                    "author": first.get("author") or d["author"], "time": first.get("time") or d["time"],
                    "time_ts": first.get("time_ts"), "text": first.get("text", ""),
                    "attachments": first.get("attachments", [])})
    return sorted(out, key=lambda x: -(x.get("time_ts") or 0))


def forum_search(client: MoodleClient, query: str, course_id: int = 1, limit: int = 30) -> list[dict]:
    """用 Moodle 自带的论坛搜索（course_id=1 表示全站）。"""
    url = f"/mod/forum/search.php?id={course_id}&search={urllib.parse.quote(query)}&perpage={limit}"
    doc = htmldom.parse(client.html(url))
    out = []
    for art in doc.find_all(data__region="post"):
        links = [a.get("href") for a in art.find_all("a") if a.get("href")]
        disc = next((h for h in links if "discuss.php?d=" in h and "#p" in h), None) \
            or next((h for h in links if "discuss.php?d=" in h), None)
        forum = next((a for a in art.find_all("a") if "/mod/forum/view.php" in a.get("href", "")), None)
        header = art.find(cls="d-flex") or art
        subject = art.find(data__region_content="forum-post-core-subject") or art.find(["h3", "h4"])
        body = art.find(id=f"post-content-{art.get('data-post-id')}") or art.find(cls="post-content-container")
        author = art.find("a", href=lambda h: "/user/view.php" in h)
        t = art.find("time")
        out.append({"id": int(art.get("data-post-id") or 0),
                    "forum": forum.text() if forum else "",
                    "subject": subject.text() if subject else header.text()[:80],
                    "author": author.text() if author else "",
                    "time": t.get("datetime") if t else None,
                    "text": body.text(sep="\n") if body else "",
                    "url": disc})
    return out


# ---------------------------------------------------------------- 通知

def alerts(client: MoodleClient, limit: int = 20) -> dict:
    client.time_remaining()  # 顺便拿 userid
    n = client.call("message_popup_get_popup_notifications",
                    {"useridto": client.userid, "limit": limit, "offset": 0, "newestfirst": True})
    counts = client.call("core_message_get_unread_conversation_counts", {"userid": client.userid})
    unread_msgs = sum((counts.get("types") or {}).values()) + (counts.get("favourites") or 0)
    return {"unread_notifications": n.get("unreadcount", 0), "unread_messages": unread_msgs,
            "notifications": [{"subject": clean(x.get("subject")), "text": clean(x.get("smallmessage")),
                               "time": _ts(x.get("timecreated")), "read": bool(x.get("read")),
                               "from": clean(x.get("userfromfullname")),
                               "url": x.get("contexturl")} for x in n.get("notifications", [])]}


# ---------------------------------------------------------------- 站内私信
#
# core_message_get_conversations / core_message_get_conversation_messages 在 Monash 上能用
# （同一个 AJAX 入口，网页右上角的消息抽屉就是用它们）。只读：这两个都不会把消息标成已读。

CONV_TYPES = {1: "private", 2: "group", 3: "self"}


def _msg(m: dict, names: dict) -> dict:
    return {"from": names.get(m.get("useridfrom"), str(m.get("useridfrom"))),
            "text": htmldom.parse(m.get("text") or "").text(sep="\n").strip(),
            "time": _ts(m.get("timecreated")), "time_ts": m.get("timecreated")}


def conversations(client: MoodleClient, limit: int = 20) -> list[dict]:
    client.time_remaining()  # 顺便拿 userid
    data = client.call("core_message_get_conversations",
                       {"userid": client.userid, "limitfrom": 0, "limitnum": limit})
    out = []
    for c in data.get("conversations", []):
        names = {u["id"]: clean(u.get("fullname")) for u in c.get("members", [])}
        others = [n for i, n in names.items() if i != client.userid]
        msgs = c.get("messages") or []
        out.append({
            "id": c["id"],
            "type": CONV_TYPES.get(c.get("type"), str(c.get("type"))),
            "name": clean(c.get("name")) or ", ".join(others) or "（自己）",
            "members": c.get("membercount"),
            "unread": c.get("unreadcount") or (0 if c.get("isread", True) else 1),
            "last": _msg(msgs[0], names | {client.userid: "我"}) if msgs else None,
        })
    return out


def conversation_messages(client: MoodleClient, conv_id: int, limit: int = 30) -> dict:
    client.time_remaining()
    data = client.call("core_message_get_conversation_messages",
                       {"currentuserid": client.userid, "convid": conv_id, "limitfrom": 0,
                        "limitnum": limit, "newest": True})
    names = {u["id"]: clean(u.get("fullname")) for u in data.get("members", [])}
    names[client.userid] = "我"
    msgs = [_msg(m, names) for m in data.get("messages", [])]
    return {"id": conv_id, "messages": sorted(msgs, key=lambda m: m["time_ts"] or 0)}


# ---------------------------------------------------------------- 查找

def _score(query: str, text: str) -> float:
    q, t = query.lower().split(), text.lower()
    if not q:
        return 0
    hits = sum(1 for w in q if w in t)
    if hits < len(q):
        # 数字要整词匹配：7 不该命中 17
        return 0
    bonus = 2 if query.lower() in t else 0
    for w in q:
        if w.isdigit() and not re.search(rf"(?<!\d){w}(?!\d)", t):
            return 0
    return hits + bonus - len(t) / 500


def find(client: MoodleClient, query: str, courses: list[dict], types: set[str] | None = None,
         limit: int = 20) -> list[dict]:
    """在课程的章节名、活动名里找。"Week 7 slides" 会同时匹配章节和活动。"""
    out = []
    for c in courses:
        state = client.course_state(c["id"])
        sections = {s["number"]: s for s in state["section"]}

        def section_path(num):
            names, seen = [], set()
            while num is not None and num in sections and num not in seen:
                seen.add(num)
                s = sections[num]
                if s["number"] != 0 and (s.get("cmlist") or not names):
                    names.append(clean(s["title"]))
                num = s.get("parent")
            return " / ".join(reversed(names))

        code = course_code(c) or clean(c.get("shortname"))
        if not types or "section" in types:
            for s in state["section"]:
                sc = _score(query, clean(s["title"]))
                if sc:
                    out.append({"score": sc + 1.5, "type": "section", "course": code, "name": clean(s["title"]),
                                "where": section_path(s.get("parent")), "url": s.get("sectionurl")})
        for cm in state["cm"]:
            if types and cm.get("module") not in types and "activity" not in types:
                continue
            path = section_path(cm.get("sectionnumber"))
            sc = _score(query, f"{clean(cm['name'])} {path}")
            if sc and cm.get("url"):
                out.append({"score": sc + (_score(query, clean(cm["name"])) > 0), "type": cm.get("module"),
                            "course": code, "name": clean(cm["name"]), "where": path,
                            "id": int(cm["id"]), "url": cm.get("url")})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------------------------------------------------------------- 单个下载

def resolve_ref(client: MoodleClient, ref: str, courses: list[dict] | None = None) -> str:
    """活动 id / Moodle URL / "FIT2102 applied 7" → 一个可下载的本站 URL。"""
    ref = ref.strip()
    if ref.isdigit():
        return f"{BASE_URL}/mod/resource/view.php?id={ref}"
    if ref.startswith("http"):
        if urllib.parse.urlsplit(ref).hostname != HOST:
            raise ValueError("只接受 learning.monash.edu 的链接")
        return ref
    unit, _, rest = ref.partition(" ")
    courses = courses or client.courses()
    course = find_course(courses, unit)
    hits = [h for h in find(client, rest or unit, [course], types={"resource", "folder", "url"}) if h.get("id")]
    if not hits:
        raise LookupError(f"{course_code(course)} 里没找到 {rest!r}")
    return hits[0]["url"]


def get(client: MoodleClient, ref: str, to: Path, force: bool = False,
        courses: list[dict] | None = None) -> list[dict]:
    """下载一个资源（或文件夹里的全部文件）到 to 目录，返回收据。"""
    url = resolve_ref(client, ref, courses)
    to.mkdir(parents=True, exist_ok=True)
    m = MOODLE_URL_RE.search(url)
    targets: list[str]
    if m and m.group(1) == "folder":
        doc = htmldom.parse(client.html(url))
        targets = [a.get("href") for a in doc.find_all("a") if "/mod_folder/content/" in a.get("href", "")]
    elif m and m.group(1) in ("resource", "url"):
        local, external, resp = client.resolve(url + ("&" if "?" in url else "?") + "redirect=1")
        if external and "cloudfront" not in external and m.group(1) == "url":
            return [{"url": external, "path": None, "note": "外部链接，没有可下载的文件"}]
        targets = [local]
    else:
        targets = [url]
    receipts = []
    for t in dict.fromkeys(targets):
        tmp = to / f".mdl-get-{abs(hash(t))}.part"
        resp = client.download(t, tmp)
        name = safe_name(filename_from(resp, "download"))
        dest = to / name
        if dest.exists() and not force:
            stem, dot, ext = name.rpartition(".")
            n = 2
            while dest.exists():
                dest = to / (f"{stem} ({n}).{ext}" if dot else f"{name} ({n})")
                n += 1
        tmp.replace(dest)
        receipts.append({"path": str(dest), "bytes": dest.stat().st_size,
                         "content_type": (resp.headers.get("Content-Type") or "").split(";")[0]})
    return receipts
