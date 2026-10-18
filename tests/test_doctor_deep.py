"""monash doctor --deep：两个来源对不上时要报出来，输出里不能有标题、文件名之类的内容。"""
import json
import time

from monash_study_kit import content_index as CI
from monash_study_kit import doctor, doctor_deep as D, features as F, jobs, moodlelib
from monash_study_kit.moodlelib import MoodleAuthError, db_connect

COURSE = {"id": 7, "fullname": "FIT2102 Programming paradigms - S2 2026", "shortname": "FIT2102"}


class FakeMoodle:
    def __init__(self, cms):
        self.cms = cms

    def courses(self):
        return [COURSE, {"id": 8, "fullname": "FIT1045 Algorithms - S1 2025", "shortname": "FIT1045"}]

    def course_state(self, course_id):
        return {"cm": self.cms, "section": []}

    def resolve(self, url):
        name = "lecture.mp4" if "id=3" in url else "notes.pdf"
        # 放在对象存储上的文件会跳到 CloudFront，那个地址没有扩展名
        return f"https://learning.monash.edu/pluginfile.php/1/{name}", "https://d1.cloudfront.net/07/90/0790a4", None


def _ctx(tmp_path, cms):
    con = db_connect(tmp_path / "moodle.db")
    con.execute("INSERT INTO courses (id, code, tracked) VALUES (7, 'FIT2102', 1), (8, 'FIT1045', 0)")
    return D.Context(client=FakeMoodle(cms), con=con)


def _cm(i, module, **kw):
    return {"id": str(i), "module": module, "url": f"https://learning.monash.edu/mod/{module}/view.php?id={i}",
            "name": f"Secret title {i}", "uservisible": True, **kw}


def test_due_dates_missing_from_the_api_are_reported(tmp_path, monkeypatch):
    now = int(time.time())
    ctx = _ctx(tmp_path, [])
    api = [{"id": 1, "kind": "due", "due_ts": now + 86400, "course": "FIT2102"}]
    ical = api + [{"id": 2, "kind": "due", "due_ts": now + 2 * 86400, "course": "FIT2102"},
                  {"id": 3, "kind": "due", "due_ts": now + 86400, "course": "FIT1045"}]   # 没跟踪的课不算
    monkeypatch.setattr(F, "load_calendar_url", lambda: "https://x/export_execute.php?authtoken=s3cret")
    monkeypatch.setattr(F, "due", lambda client, days, include_overdue_days: api)
    monkeypatch.setattr(F, "fetch_ical", lambda url: "")
    monkeypatch.setattr(F, "ical_items", lambda text, now: ical)
    r = D.deep_due(ctx)
    assert r["status"] == doctor.WARN and "接口 1 条，日历订阅 2 条" in r["detail"] and "FIT2102" in r["detail"]
    assert "s3cret" not in json.dumps(r)
    monkeypatch.setattr(F, "due", lambda client, days, include_overdue_days: ical[:2])
    assert D.deep_due(ctx)["status"] == doctor.OK


def test_assignment_page_parser_is_checked_against_course_structure(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, [_cm(10, "assign"), _cm(11, "assign"), _cm(12, "assign", uservisible=False)])
    monkeypatch.setattr(F, "assignments", lambda client, course: [{"cmid": 10}])
    r = D.deep_assignments(ctx)
    assert r["status"] == doctor.WARN and "cmid 11" in r["detail"] and "12" not in r["detail"]
    assert "Secret" not in r["detail"]
    monkeypatch.setattr(F, "assignments", lambda client, course: [{"cmid": 10}, {"cmid": 11}])
    assert D.deep_assignments(ctx)["status"] == doctor.OK


def test_files_compare_course_structure_with_local_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(D, "load_settings", lambda: {"download_videos": False})
    (tmp_path / "files" / "FIT2102").mkdir(parents=True)
    (tmp_path / "files" / "FIT2102" / "w1.pdf").write_bytes(b"x")
    ctx = _ctx(tmp_path, [_cm(1, "resource"), _cm(2, "resource"), _cm(3, "resource"), _cm(4, "forum"),
                          _cm(5, "resource", stealth=True)])                # 课程页上不显示，同步不下
    add = "INSERT INTO files (course_id, source, cmid, path, synced_at) VALUES (7, ?, ?, ?, 'x')"
    ctx.con.execute(add, ("s1", 1, "FIT2102/w1.pdf"))
    ctx.con.execute(add, ("s9", 9, "FIT2102/old.pdf"))          # Moodle 上已经没有了，磁盘上也没有
    r = D.deep_files(ctx)
    assert r["status"] == doctor.WARN
    assert "1 个是没下载的录像" in r["detail"]                    # cmid 3 是录像，默认不下载，不算漏
    assert "本地没有：FIT2102 cmid 2" in r["detail"] and "FIT2102 cmid 9" in r["detail"]
    assert "磁盘上找不到的文件 1 个" in r["detail"] and "old.pdf" not in r["detail"]
    assert "cmid 5" not in r["detail"] and "3 个文件资源" in r["detail"]


def test_index_check_searches_back_the_original_file(tmp_path, monkeypatch):
    files = tmp_path / "files"
    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "ed-files")
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path / "data")
    (files / "FIT2102 Paradigms" / "Week 1").mkdir(parents=True)
    (files / "FIT2102 Paradigms" / "Week 1" / "notes.md").write_text("Haskell typeclasses and functors", encoding="utf-8")
    ctx = _ctx(tmp_path, [])
    assert D.deep_index(ctx)["status"] == doctor.WARN               # 还没建索引
    CI.index(ctx.con)
    r = D.deep_index(ctx)
    assert r["status"] == doctor.OK and "抽查搜索 1/1" in r["detail"], r
    ctx.con.execute("DELETE FROM content_fts")                      # 索引坏了：记录还在，内容没了
    assert D.deep_index(ctx)["status"] == doctor.FAIL


def test_index_probe_ignores_words_inside_links(tmp_path, monkeypatch):
    files = tmp_path / "files"
    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "ed-files")
    monkeypatch.setattr(moodlelib, "DATA_DIR", tmp_path / "data")
    (files / "FIT3199 Project" / "Week 1").mkdir(parents=True)
    (files / "FIT3199 Project" / "Week 1" / "links.md").write_text(
        "- [Plan](https://docs.google.com/document/d/1xQwErTyUiOpAsDfGhJkLzXcVbNm/edit) team plan\n"
        "- Reading 9AbCdEfGhIjKlMn here\n", encoding="utf-8")
    ctx = _ctx(tmp_path, [])
    CI.index(ctx.con)
    r = D.deep_index(ctx)
    assert r["status"] == doctor.OK, r
    (files / "FIT3199 Project" / "Week 1" / "links.md").write_text("only https://x.io/AbcdefghijKlmnop", encoding="utf-8")
    CI.index(ctx.con)
    assert "抽查搜索 0/0" in D.deep_index(ctx)["detail"]


def test_ed_check_finds_threads_missing_or_outdated_locally(tmp_path, monkeypatch):
    from monash_study_kit import edlib, edsync
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses (id, code, tracked) VALUES (5, 'FIT2102 S2 2026', 1)")
    fresh = {"id": 100, "number": 1, "updated_at": "a", "title": "t"}
    conn.execute("INSERT INTO threads (id, course_id, content_hash) VALUES (100, 5, ?)", (edsync.content_stamp(fresh),))
    conn.execute("INSERT INTO threads (id, course_id, content_hash) VALUES (101, 5, 'old')")
    conn.commit()
    live = [fresh, {"id": 101, "number": 2, "updated_at": "b", "title": "t"}, {"id": 102, "number": 3, "title": "t"}]

    class FakeEd:
        def get(self, path, params):
            return {"threads": live}
    monkeypatch.setattr(jobs, "have_ed_token", lambda: True)
    monkeypatch.setattr(edlib, "EdClient", FakeEd)
    real_connect = edlib.db_connect
    monkeypatch.setattr(edlib, "db_connect", lambda: real_connect(tmp_path / "ed.db"))
    r = D.deep_ed(_ctx(tmp_path, []))
    assert r["status"] == doctor.WARN and "本地没有：FIT2102#3" in r["detail"] and "本地是旧的：FIT2102#2" in r["detail"]


def test_one_failing_check_does_not_stop_the_rest(tmp_path, monkeypatch):
    def boom(ctx):
        raise MoodleAuthError("还没登录 Moodle")

    def crash(ctx):
        raise RuntimeError("GET https://x/?authtoken=abc failed")
    monkeypatch.setattr(D, "DEEP_CHECKS", [boom, crash])
    monkeypatch.setattr(D, "NAMES", {"boom": "A", "crash": "B"})
    monkeypatch.setattr(D, "db_connect", lambda: db_connect(tmp_path / "m.db"))
    out = D.run()
    assert [r["status"] for r in out] == [doctor.FAIL, doctor.WARN] and out[0]["fix"] == "monash login"
    assert "abc" not in out[1]["detail"]


def test_ed_posts_changed_after_the_last_sync_are_not_reported(tmp_path, monkeypatch):
    from monash_study_kit import edlib
    conn = edlib.db_connect(tmp_path / "ed.db")
    conn.execute("INSERT INTO courses (id, code, tracked) VALUES (5, 'FIT2102 S2 2026', 1)")
    conn.execute("INSERT INTO threads (id, course_id, content_hash) VALUES (101, 5, 'old'), (103, 5, 'old')")
    conn.execute("INSERT INTO runs (started_at, finished_at) VALUES ('2026-10-18T18:01:00+00:00', '2026-10-18T18:02:00+00:00')")
    conn.commit()
    live = [{"id": 101, "number": 2, "updated_at": "2026-10-19T05:20:00.123456+11:00", "title": "t"},  # 18:20 UTC
            {"id": 102, "number": 3, "created_at": "2026-10-18T18:30:00Z", "title": "t"},
            {"id": 103, "number": 4, "updated_at": "2026-10-18T17:00:00Z", "title": "t"}]

    class FakeEd:
        def get(self, path, params):
            return {"threads": live}
    monkeypatch.setattr(jobs, "have_ed_token", lambda: True)
    monkeypatch.setattr(edlib, "EdClient", FakeEd)
    real_connect = edlib.db_connect
    monkeypatch.setattr(edlib, "db_connect", lambda: real_connect(tmp_path / "ed.db"))
    r = D.deep_ed(_ctx(tmp_path, []))
    assert r["status"] == doctor.WARN and "2 个是上次同步之后才有变化的" in r["detail"]
    assert "本地是旧的：FIT2102#4" in r["detail"] and "#2" not in r["detail"] and "#3" not in r["detail"]
