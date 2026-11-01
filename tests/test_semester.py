from datetime import datetime, timezone

from monash_study_kit import cli, semester


def fixture():
    return {
        "days": 7,
        "generated_at": "2026-11-01T00:00:00+00:00",
        "due": [
            {"id": 1, "due": "2026-11-03T08:00:00+00:00", "due_ts": 1793692800,
             "course": "FIT2102", "name": "Assignment", "source": "api"},
            {"id": 1, "due": "2026-11-03T08:00:00+00:00", "due_ts": 1793692800,
             "course": "FIT2102", "name": "Assignment", "source": "api"},
            {"id": 2, "due": "", "due_ts": None, "course": "FIT9999", "name": "Unknown", "source": "ical"},
        ],
        "possibly_missing": [], "ed_lessons": [{"course": "FIT2109", "title": "W3 workshop", "status": "unattempted"}],
        "ed_unread_replies": [{"ref": "FIT2109#12", "title": "Help", "new": 2}],
        "announcements": [], "notes": ["Moodle session expired"],
    }


def test_week_view_keeps_data_sources_and_local_attendance(monkeypatch):
    monkeypatch.setattr(semester, "load_settings", lambda: {})
    view = semester.semester_data(fixture(), {
        "records": [{"date": "2026-11-03", "course": "FIT2102", "session": "Applied", "status": "pending"}]
    })
    assert len(view["deadlines"]) == 3
    assert view["ed_lessons"][0]["course"] == "FIT2109"
    assert view["attendance"][0]["status"] == "pending"
    assert "考勤" in semester.render(view)
    assert any("缓存" in n for n in view["notes"])


def test_private_ics_deduplicates_and_skips_unverified_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(semester, "HOME", tmp_path)
    monkeypatch.setattr(semester, "load_settings", lambda: {})
    view = semester.semester_data(fixture())
    text = semester.calendar_ics(view, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert text.count("BEGIN:VEVENT") == 1
    assert "DTSTART:20261103T080000Z" in text
    assert "DTEND:20261103T080500Z" in text
    assert "FIT2102" in text
    assert "FIT9999" not in text
    assert "https://" not in text
    path = semester.write_calendar(view)
    assert path == tmp_path / "semester.ics"
    assert path.exists()
    assert path.read_text(encoding="utf-8").count("BEGIN:VEVENT") == 1


def test_ics_does_not_follow_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(semester, "HOME", tmp_path)
    target = tmp_path / "external.ics"
    target.write_text("not changed", encoding="utf-8")
    (tmp_path / "semester.ics").symlink_to(target)
    import pytest
    with pytest.raises(ValueError):
        semester.write_calendar({"deadlines": []})
    assert target.read_text() == "not changed"


def test_semester_cli_parser():
    args = cli.build_parser().parse_args(["semester", "--days", "5", "--ics"])
    assert args.days == 5 and args.ics == ""
