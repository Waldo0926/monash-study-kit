"""Attendance session checks and local-only silent SSO recovery.

The persistent browser profile stays on the login machine. Only portal application
state is exported by attendance._save_state; no passwords, MFA or IdP state are pushed.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from . import attendance as A
from .paths import DATA_DIR, load_settings, write_secret

STATUS_FILE = DATA_DIR / 'attendance' / 'authentication.json'


def read_status():
    try:
        data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp():
    try:
        info = A.STATE_FILE.stat()
        return [info.st_mtime_ns, info.st_size]
    except OSError:
        return None


def record(status, **extra):
    data = {'status': status, 'checked_at': datetime.now(A.TZ).isoformat(),
            'application_state_stamp': _stamp(), **extra}
    write_secret(STATUS_FILE, json.dumps(data))
    return data


def _require_personal_mode():
    if load_settings().get('materials_only'):
        raise PermissionError('只导课程资料模式下不能维护个人考勤登录')


def renew(timeout=30):
    """Try the existing local SSO profile; never enter credentials or answer MFA."""
    _require_personal_mode()
    if not (A.HOME / 'attendance-browser').is_dir():
        raise A.AttendanceAuthError('No local SSO profile; run monash attendance login on your computer')
    timeout = max(1, min(float(timeout), 120))
    deadline = time.monotonic() + timeout
    with A.persistent_browser(headless=True) as context:
        page = context.new_page()
        page.goto(A.ORIGIN + '/student/Default.aspx', wait_until='domcontentloaded', timeout=int(timeout * 1000))
        while time.monotonic() < deadline:
            if page.is_closed():
                break
            try:
                A._check_page(page)
            except A.AttendanceAuthError:
                pass
            else:
                from . import attendance_auto, attendance_http
                with attendance_http.portal_lock():
                    A._save_state(context)
                    attendance_auto.record_login()
                    return record('recovered', authenticated=True, method='local_sso')
            page.wait_for_timeout(500)
    raise A.AttendanceAuthError('SSO requires interactive sign-in or MFA; run monash attendance login')


def maintain(force=False):
    """Probe valid app sessions, then try local SSO once; persist a retry cooldown."""
    _require_personal_mode()
    old = read_status()
    same_state = old.get('application_state_stamp') == _stamp()
    next_probe = old.get('next_probe_at', 0)
    if not isinstance(next_probe, (int, float)):
        next_probe = 0
    if not force and same_state and old.get('status') == 'needs_login' and time.time() < next_probe:
        return {**old, 'checked': False}
    if A.STATE_FILE.is_file():
        try:
            from . import attendance_http
            try:
                attendance_http.keepalive()
            except attendance_http.FastPathUnavailable:
                A.keepalive()
        except A.AttendanceAuthError:
            pass
        else:
            if old.get('status') == 'needs_login':
                from . import attendance_auto
                attendance_auto.record_login()
            return record('authenticated', authenticated=True, checked=True)
    try:
        return renew()
    except A.AttendanceAuthError:
        # An unavailable IdP session is not recovered by repeatedly using an expired app cookie.
        return record('needs_login', authenticated=False, checked=True,
                      next_probe_at=time.time() + 3600,
                      action='Run monash attendance login locally; complete Monash sign-in/MFA')
