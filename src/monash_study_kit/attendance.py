"""Attendance portal adapter with isolated application state and verified writes."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .paths import DATA_DIR, HOME, SECRETS_DIR, ensure_private_dir, write_secret

# The parser and day menu were verified against the Malaysia campus portal. Other campuses can
# point these at their own portal and timezone, but expect to check the page structure first.
ORIGIN = os.environ.get("MONASH_ATTENDANCE_ORIGIN", "https://attendance.monash.edu.my").rstrip("/")
STATE_FILE = SECRETS_DIR / "attendance-state.json"
CACHE_FILE = DATA_DIR / "attendance" / "records.json"
ASSETS = Path(__file__).with_name("attendance_assets")
TZ = ZoneInfo(os.environ.get("MONASH_ATTENDANCE_TZ", "Asia/Kuala_Lumpur"))


class AttendanceAuthError(RuntimeError):
    pass


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Install monash-study-kit[attendance] and run: python -m playwright install chromium") from e
    return sync_playwright


def _save_state(context):
    state = context.storage_state()
    state["cookies"] = context.cookies([ORIGIN + "/student/"])
    state["origins"] = [o for o in state.get("origins", []) if o["origin"] == ORIGIN]
    write_secret(STATE_FILE, json.dumps(state))


def login(timeout=600):
    """User-facing login helper; never handles a password or automates MFA."""
    profile = HOME / "attendance-browser"
    ensure_private_dir(profile)
    with _playwright()() as p:
        from monash_study_kit.browser_login import find_browser

        executable = find_browser()
        context = p.chromium.launch_persistent_context(
            str(profile), headless=False, timezone_id=str(TZ), **({"executable_path": executable} if executable else {})
        )
        try:
            page = context.new_page()
            page.goto(ORIGIN + "/student/Default.aspx", wait_until="domcontentloaded")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise AttendanceAuthError("Login window closed before completion")
                if page.url.startswith(ORIGIN + "/student/"):
                    clue = page.locator("body").inner_text(timeout=10000)
                    if re.search(r"attendance|semester|units|logout|log out", clue, re.I):
                        _save_state(context)
                        return {"status": "connected", "saved": True}
                page.wait_for_timeout(1000)
            raise AttendanceAuthError("Login timed out; complete Monash sign-in and MFA in the browser")
        finally:
            context.close()


@contextmanager
def browser():
    if not STATE_FILE.is_file():
        raise AttendanceAuthError(
            "Complete Attendance login in the local setup helper and push application state to the VPS"
        )
    ensure_private_dir(CACHE_FILE.parent)
    lock = sqlite3.connect(CACHE_FILE.parent / "session-lock.db", timeout=2)
    lock.execute("CREATE TABLE IF NOT EXISTS lock (id INTEGER)")
    try:
        lock.execute("BEGIN IMMEDIATE")
        with _playwright()() as p:
            instance = p.chromium.launch(headless=True)
            context = instance.new_context(storage_state=str(STATE_FILE), timezone_id=str(TZ))
            try:
                yield context
            finally:
                context.close()
                instance.close()
    finally:
        lock.rollback()
        lock.close()


def _check_page(page):
    if not page.url.startswith(ORIGIN + "/student/"):
        raise AttendanceAuthError("Attendance session expired; reconnect with Monash sign-in")
    text = page.locator("body").inner_text(timeout=15000)
    if re.search(r"sign in to|登录您的帐户|session.*expired", text, re.I):
        raise AttendanceAuthError("Attendance session expired; reconnect with Monash sign-in")
    return text


def _open_day(page, key):
    """Show one date's panel. The portal ignores the URL hash: days are switched with the
    #daySel menu (values like 31_Oct_26), which reveals div#dayPanel_<key>. Returns False
    when the portal does not offer that date (it lists about two weeks around today)."""
    page.goto(ORIGIN + "/student/Units.aspx", wait_until="domcontentloaded", timeout=45000)
    _check_page(page)
    page.wait_for_selector("#daySel", state="attached", timeout=15000)
    if key not in page.evaluate("() => [...document.querySelectorAll('#daySel option')].map(o => o.value)"):
        return False
    page.select_option("#daySel", key)
    page.wait_for_function(
        "key => { const p=document.getElementById('dayPanel_'+key); return p && getComputedStyle(p).display !== 'none'; }",
        arg=key,
        timeout=15000,
    )
    return True


def _dates(days):
    if not 1 <= int(days) <= 180:
        raise ValueError("days must be between 1 and 180")
    today = datetime.now(TZ).date()
    return [today - timedelta(days=i) for i in range(int(days) - 1, -1, -1)]


def _load():
    try:
        return json.loads(CACHE_FILE.read_text())
    except FileNotFoundError:
        return {"records": [], "last_success": None, "scanned_dates": []}


def status():
    cache = _load()
    return {
        "configured": STATE_FILE.is_file(),
        "last_success": cache.get("last_success"),
        "cached_records": len(cache["records"]),
        "scanned_dates": cache.get("scanned_dates", []),
        "last_error": cache.get("last_error"),
        "session_lifetime": "Not yet measured against the real account",
    }


def keepalive():
    with browser() as context:
        page = context.new_page()
        page.goto(ORIGIN + "/student/Default.aspx", wait_until="domcontentloaded", timeout=45000)
        _check_page(page)
        _save_state(context)
    return {"authenticated": True, "checked_at": datetime.now(TZ).isoformat()}


def sync(days=7):
    if not 1 <= int(days) <= 14:
        raise ValueError("A sync job may scan at most 14 days; use separate historical batches")
    script = (ASSETS / "extract-rows.js").read_text()
    dates = _dates(days)
    # Include published upcoming sessions through Sunday, without projecting missing rows.
    today = datetime.now(TZ).date()
    dates += [today + timedelta(days=i) for i in range(1, 7 - today.weekday())]
    records, scanned, unavailable = [], [], []
    try:
        with browser() as context:
            page = context.new_page()
            for date in dates:
                month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][
                    date.month - 1
                ]
                key = f"{date.day}_{month}_{date.strftime('%y')}"
                # The requested date's panel must be the visible one; a generic body is not sufficient.
                if not _open_day(page, key):
                    unavailable.append(date.isoformat())
                    continue
                rows = page.evaluate(script, key)
                if not rows.get("ready"):
                    raise RuntimeError("Attendance date page was not ready")
                for row in rows["sessions"]:
                    row["date"] = date.isoformat()
                    row["attendanceDate"] = {"iso": date.isoformat(), "key": key}
                    row["id"] = hashlib.sha256(
                        json.dumps([row["date"], row["course"], row["session"], row["time"]]).encode()
                    ).hexdigest()[:24]
                    row["observed_at"] = datetime.now(TZ).isoformat()
                    records.append(row)
                scanned.append(date.isoformat())
            _save_state(context)
        old = _load()
        merged = {row["id"]: row for row in old["records"] if row["date"] not in scanned}
        merged.update({row["id"]: row for row in records})
        cache = {
            "records": list(merged.values()),
            "scanned_dates": sorted(set(old.get("scanned_dates", [])) | set(scanned)),
            "last_success": datetime.now(TZ).isoformat(),
            "last_error": None,
        }
        write_secret(CACHE_FILE, json.dumps(cache))
        return {
            "read_records": len(records),
            "scanned_dates": scanned,
            "unavailable_dates": unavailable,
            "status": "ok",
        }
    except Exception as e:
        cache = _load()
        cache["last_error"] = type(e).__name__ + ": " + str(e)[:250]
        write_secret(CACHE_FILE, json.dumps(cache))
        raise


def overview(days=14):
    dates = {d.isoformat() for d in _dates(days)}
    today = datetime.now(TZ).date().isoformat()
    cache = _load()
    records = [
        {**r, "status": "completed" if r["completed"] else "future" if r["date"] > today else "pending"}
        for r in cache["records"]
        if r["date"] in dates or r["date"] > today
    ]
    return {
        "records": sorted(records, key=lambda r: (r["date"], r["course"], r["time"])),
        "coverage": status(),
        "instruction": "Absence from this cache is not proof of completed attendance",
    }


def find_codes(days=7, gmail_allowed=False):
    from . import attendance_evidence

    rows = overview(days)["records"]
    pending = [r for r in rows if not r["completed"]]
    evidence = attendance_evidence.collect(pending, gmail_allowed=gmail_allowed)
    result = {r["id"]: {**r, "candidates": []} for r in pending}
    shared = (ASSETS / "shared.js").read_text()
    matcher = (
        "({text,items}) => {" + re.sub(r"\bexport\s+", "", shared) + ";return matchCodesToAttendance(text,items);}"
    )
    with _playwright()() as p:
        browser_instance = p.chromium.launch(headless=True)
        try:
            page = browser_instance.new_page()
            for source in evidence["sources"]:
                course = source.get("course", "")
                scoped = [r for r in pending if not course or r["course"].upper() == course.upper()]
                for match in page.evaluate(matcher, {"text": source["text"], "items": scoped}):
                    if match.get("code"):
                        result[match["id"]]["candidates"].append(
                            {
                                "code": match["code"],
                                "confidence": match["confidence"],
                                "context": match["context"],
                                "source": source["url"],
                                "kind": source["kind"],
                            }
                        )
        finally:
            browser_instance.close()
    for row in result.values():
        codes = {c["code"] for c in row["candidates"]}
        row["confidence"] = "conflict" if len(codes) > 1 else "review" if codes else "missing"
    return {"records": list(result.values()), "coverage": evidence["coverage"], "portal": status()}


def prepare(record_id, code, actually_attended):
    if actually_attended is not True:
        raise ValueError("The user must explicitly declare they actually attended")
    if not re.fullmatch(r"[A-Za-z0-9]{5}", code):
        raise ValueError("Attendance code must be five alphanumeric characters")
    row = next((r for r in _load()["records"] if r["id"] == record_id), None)
    if not row or row["completed"]:
        raise ValueError("Record is missing or already complete; refresh Attendance")
    if not row.get("entryUrl", "").startswith(ORIGIN + "/student/"):
        raise ValueError("Record has no verified Attendance entry URL")
    started = datetime.strptime(row["date"] + " " + row["time"].replace(" ", ""), "%Y-%m-%d %I:%M%p").replace(tzinfo=TZ)
    if started > datetime.now(TZ):
        raise ValueError("The class has not started")
    return {"record": row, "code": code.upper(), "actually_attended": True}


def submit(payload):
    row, code = payload["record"], payload["code"]
    prepare(row["id"], code, payload.get("actually_attended"))
    script = (ASSETS / "extract-rows.js").read_text()
    key = row["attendanceDate"]["key"]
    with browser() as context:
        page = context.new_page()

        def reread():
            if not _open_day(page, key):
                return None
            rows = page.evaluate(script, key)["sessions"]
            return next(
                (
                    r
                    for r in rows
                    if r["course"] == row["course"] and r["session"] == row["session"] and r["time"] == row["time"]
                ),
                None,
            )

        current = reread()
        if not current:
            raise ValueError("Portal no longer contains this class; refresh and review again")
        if current["completed"]:
            return {"verified": True, "already_completed": True}
        entry = current["entryUrl"]
        if not entry.startswith(ORIGIN + "/student/"):
            raise ValueError("Unexpected Attendance entry URL")
        page.goto(entry, wait_until="domcontentloaded")
        _check_page(page)
        fields = page.locator("input[type=text]:visible,input:not([type]):visible,input[type=tel]:visible")
        if fields.count() != 1:
            raise ValueError("Attendance input is ambiguous; use the portal manually")
        fields.fill(code)
        button = page.get_by_role("button", name=re.compile(r"submit|confirm|record|save|enter", re.I))
        if button.count() != 1:
            raise ValueError("Attendance submission button is ambiguous; use the portal manually")
        button.click()
        page.wait_for_load_state("domcontentloaded")
        verified = reread()
        _save_state(context)
        if not verified or not verified["completed"]:
            raise RuntimeError("Submission result is unverified; check the portal before retrying")
    cache = _load()
    for item in cache["records"]:
        if item["id"] == row["id"]:
            item["completed"] = True
            item["entryUrl"] = ""
    write_secret(CACHE_FILE, json.dumps(cache))
    return {"verified": True, "record_id": row["id"]}


def export_history(days=120):
    data = overview(days)
    directory = DATA_DIR / "attendance" / "exports"
    ensure_private_dir(directory)
    target = directory / ("attendance-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + ".csv")
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["date", "course", "session", "time", "status", "observed_at"])
    writer.writeheader()
    for row in data["records"]:
        writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
    write_secret(target, "\ufeff" + out.getvalue())
    return {"path": str(target), "rows": len(data["records"]), "coverage": data["coverage"]}
