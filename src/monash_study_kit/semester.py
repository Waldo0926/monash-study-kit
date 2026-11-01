"""Local-first semester view and private iCalendar export.

This deliberately reuses todo.build, not a new account/sync service. Due
dates come from Moodle or the user's configured Moodle iCal fallback. Ed
lesson progress, replies and announcements come from the local synced DB.
Attendance is optional and reads the local attendance cache. Nothing is sent
to Monash Hub. Missing data stays missing; neither events nor check-ins
imply official completion.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import HOME, load_settings

def semester_data(todo_data: dict, attendance_data: dict | None = None) -> dict:
    data = {
        "days": todo_data.get("days", 7),
        "generated_at": todo_data.get("generated_at"),
        "deadlines": sorted(todo_data.get("due", []), key=lambda x: x.get("due_ts") or 0),
        "possibly_missing": todo_data.get("possibly_missing", []),
        "ed_lessons": todo_data.get("ed_lessons", []),
        "ed_unread_replies": todo_data.get("ed_unread_replies", []),
        "announcements": todo_data.get("announcements", []),
        "attendance": (attendance_data or {}).get("records", []),
        "notes": list(todo_data.get("notes", [])),
    }
    if attendance_data is not None:
        data["notes"].append("考勤只显示已同步到本机的班次，缓存中没有记录不等于已经签到")
    elif load_settings().get("attendance_enabled"):
        data["notes"].append("考勤缓存暂时不可用，请运行 monash attendance status")
    return data

def render(data: dict) -> str:
    lines = [f"== 未来 {data['days']} 天的学期概览 =="]
    for note in data["notes"]:
        lines.append(f"! {note}")
    lines.append(f"\n截止日期（{len(data['deadlines'])}）")
    for item in data["deadlines"]:
        lines.append(f"  {item.get('due', '?')} [{item.get('course','?')}] {item.get('activity') or item.get('name','')}")
    lines.append(f"\n可能漏交（{len(data['possibly_missing'])}）")
    for item in data["possibly_missing"]:
        lines.append(f"  [{item.get('course','?')}] {item.get('name','')}")
    lines.append(f"\nEd未完成Lesson（{len(data['ed_lessons'])}）")
    for item in data["ed_lessons"]:
        lines.append(f"  [{item.get('course','?')}] {item.get('title','')} ({item.get('status','')})")
    lines.append(f"\nEd有新回复的讨论（{len(data['ed_unread_replies'])}）")
    for item in data["ed_unread_replies"]:
        lines.append(f"  {item.get('ref','')} {item.get('title','')}")
    lines.append(f"\n近期公告（{len(data['announcements'])}）")
    for item in data["announcements"]:
        lines.append(f"  [{item.get('course','?')}] {item.get('title','')}")
    if data["attendance"]:
        lines.append(f"\n本地考勤缓存（{len(data['attendance'])}）")
        for item in data["attendance"]:
            lines.append(f"  {item.get('date','')} [{item.get('course','?')}] {item.get('session','')}：{item.get('status','unknown')}")
    return "\n".join(lines)

def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")

def _fold(line: str) -> list[str]:
    chunks, part, length = [], "", 0
    for char in line:
        n = len(char.encode("utf-8"))
        if length + n > 75:
            chunks.append(part)
            part, length = " ", 1
        part, length = part + char, length + n
    return chunks + ([part] if part else [])

def _stamp(raw: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (TypeError, ValueError):
        return None

def calendar_ics(data: dict, now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    when = lambda dt: dt.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Monash Study Kit//Private semester export//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:My Monash deadlines"]
    seen = set()
    for entry in data.get("deadlines", []):
        if not isinstance(entry, dict):
            continue
        due = _stamp(entry.get("due", ""))
        if due is None:
            continue
        # A timestamp is essential: never invent a deadline from a title.
        key = (str(entry.get("id") or ""), entry.get("course") or "", when(due),
               entry.get("activity") or entry.get("name") or "")
        if key in seen:
            continue
        seen.add(key)
        import hashlib
        uid = hashlib.sha256(repr(key).encode()).hexdigest()[:24] + "@local.monash-study-kit"
        title = "[" + str(entry.get("course") or "Monash") + "] " + str(entry.get("activity") or entry.get("name") or "Deadline")
        description = "Local calendar export; check Moodle before relying on this deadline."
        if entry.get("source") == "ical":
            description += " Source: imported Moodle calendar, submission status unavailable."
        lines += ["BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + when(now),
                  "DTSTART:" + when(due), "DTEND:" + when(due + timedelta(minutes=5)),
                  "SUMMARY:" + _escape(title), "DESCRIPTION:" + _escape(description), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(item for line in lines for item in _fold(line)) + "\r\n"

def write_calendar(data: dict, path: str | Path | None = None) -> Path:
    target = Path(path) if path else HOME / "semester.ics"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("不写入符号链接，避免覆盖其他文件")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(calendar_ics(data))
        try:
            target.chmod(0o600)
        except OSError:
            pass  # Windows ACLs are controlled by the user's profile.
    except BaseException:
        raise
    return target
