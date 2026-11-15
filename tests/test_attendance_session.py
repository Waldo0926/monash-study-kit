import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from monash_study_kit import attendance as A, attendance_session as S, attendance_http as H, attendance_auto


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(A, 'HOME', tmp_path)
    monkeypatch.setattr(A, 'STATE_FILE', tmp_path / 'application.json')
    monkeypatch.setattr(S, 'STATUS_FILE', tmp_path / 'auth.json')
    monkeypatch.setattr(attendance_auto, 'SESSION_FILE', tmp_path / 'lifetime.json')
    monkeypatch.setattr(S, 'load_settings', lambda: {})
    return tmp_path


def test_failed_session_is_throttled_and_replaced_state_is_rechecked(session, monkeypatch):
    monkeypatch.setattr(S, 'renew', Mock(side_effect=A.AttendanceAuthError('MFA required')))
    first = S.maintain()
    assert first['status'] == 'needs_login'
    assert S.maintain()['checked'] is False
    S.renew.assert_called_once()
    A.STATE_FILE.write_text('{}')
    check = Mock(return_value={'authenticated': True})
    monkeypatch.setattr(H, 'keepalive', check)
    assert S.maintain()['status'] == 'authenticated'
    check.assert_called_once()


def test_tls_fast_path_failure_uses_the_browser(session, monkeypatch):
    A.STATE_FILE.write_text('{}')
    monkeypatch.setattr(H, 'keepalive', Mock(side_effect=H.FastPathUnavailable('certificate chain')))
    browser = Mock(return_value={'authenticated': True})
    monkeypatch.setattr(A, 'keepalive', browser)
    assert S.maintain()['authenticated'] is True
    browser.assert_called_once()


def test_network_failures_are_not_classified_as_needing_login(session, monkeypatch):
    A.STATE_FILE.write_text('{}')
    monkeypatch.setattr(H, 'keepalive', Mock(side_effect=OSError('network down')))
    monkeypatch.setattr(S, 'renew', Mock())
    with pytest.raises(OSError):
        S.maintain()
    assert S.read_status() == {}
    S.renew.assert_not_called()


def test_expired_application_attempts_silent_local_recovery(session, monkeypatch):
    A.STATE_FILE.write_text('{}')
    monkeypatch.setattr(H, 'keepalive', Mock(side_effect=A.AttendanceAuthError('expired')))
    recovery = Mock(return_value={'status': 'recovered', 'authenticated': True})
    monkeypatch.setattr(S, 'renew', recovery)
    assert S.maintain()['status'] == 'recovered'
    recovery.assert_called_once()


class Page:
    def __init__(self, authenticated=True):
        self.url = A.ORIGIN + '/student/Default.aspx' if authenticated else 'https://monashuni.okta.com/'
    def goto(self, *args, **kwargs):
        pass
    def is_closed(self):
        return False
    def locator(self, *args):
        return self
    def inner_text(self, **kwargs):
        return 'Attendance Units'
    def wait_for_timeout(self, value):
        pass


class Context:
    def __init__(self, page):
        self.page = page
    def new_page(self):
        return self.page
    def storage_state(self):
        return {'cookies': [{'name': 'SSO', 'value': 'synthetic'}],
                'origins': [{'origin': A.ORIGIN, 'localStorage': []},
                            {'origin': 'https://monashuni.okta.com', 'localStorage': []}]}
    def cookies(self, urls):
        assert urls == [A.ORIGIN + '/student/']
        return [{'name': 'application', 'value': 'synthetic-app'}]


def mock_profile(monkeypatch, page):
    @contextmanager
    def profile(headless=False):
        assert headless is True
        yield Context(page)
    monkeypatch.setattr(A, 'persistent_browser', profile)


def test_sso_recovery_exports_only_portal_application_state(session, monkeypatch):
    (session / 'attendance-browser').mkdir()
    mock_profile(monkeypatch, Page())
    assert S.renew()['status'] == 'recovered'
    exported = json.loads(A.STATE_FILE.read_text())
    assert [c['name'] for c in exported['cookies']] == ['application']
    assert [o['origin'] for o in exported['origins']] == [A.ORIGIN]
    assert S.read_status()['status'] == 'recovered'


def test_mfa_required_preserves_previous_application_state(session, monkeypatch):
    (session / 'attendance-browser').mkdir()
    A.STATE_FILE.write_text('old application state')
    mock_profile(monkeypatch, Page(authenticated=False))
    times = iter([0, 0.1, 2])
    monkeypatch.setattr(S.time, 'monotonic', lambda: next(times))
    with pytest.raises(A.AttendanceAuthError, match='MFA'):
        S.renew(timeout=1)
    assert A.STATE_FILE.read_text() == 'old application state'


def test_materials_mode_cannot_recover_personal_sessions(session, monkeypatch):
    monkeypatch.setattr(S, 'load_settings', lambda: {'materials_only': True})
    with pytest.raises(PermissionError):
        S.maintain()
    with pytest.raises(PermissionError):
        S.renew()
