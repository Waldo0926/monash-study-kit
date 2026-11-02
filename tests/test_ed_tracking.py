"""Ed 课程按已跟踪的 Moodle 课号自动匹配，保留用户修改。"""
from monash_study_kit import edlib, edsync, moodlelib


def test_archived_ed_course_matches_tracked_moodle_and_preserves_choice(tmp_path):
    m=moodlelib.db_connect(tmp_path/"m.db")
    e=edlib.db_connect(tmp_path/"e.db")
    m.execute("INSERT INTO courses (id,code,fullname,tracked) VALUES (1,'FIT1045','Sample',1)")
    courses=[{"id":7,"code":"FIT1045/FIT1053 S1 2025","name":"Sample", "year":"2025", "session":"S1", "status":"archived"},
             {"id":8,"code":"FIT9999 S1 2025","name":"Other", "year":"2025", "session":"S1", "status":"active"}]
    edsync.upsert_courses(e,courses)
    assert [c["id"] for c in edsync.auto_track(e,courses,m)] == [7]
    assert e.execute("SELECT tracked FROM courses WHERE id=8").fetchone()[0] == 0
    e.execute("UPDATE courses SET tracked=0 WHERE id=7")
    assert edsync.auto_track(e,courses,m) == []
    assert e.execute("SELECT tracked FROM courses WHERE id=7").fetchone()[0] == 0
    courses.append({**courses[0],"id":9})
    edsync.upsert_courses(e,courses)
    assert [c["id"] for c in edsync.auto_track(e,courses,m)] == [9]
    m.close()
    e.close()


def test_nothing_is_marked_seen_before_any_moodle_unit_is_tracked(tmp_path):
    """第一次 setup 时 Ed 先同步、Moodle 还没选课：不记“看过”，先跟踪 active 的课；选了 Moodle 课以后再匹配。"""
    m = moodlelib.db_connect(tmp_path / "m.db")
    e = edlib.db_connect(tmp_path / "e.db")
    courses = [{"id": 7, "code": "FIT1045 S1 2025", "name": "Old", "year": "2025", "session": "S1", "status": "archived"},
               {"id": 8, "code": "FIT2102 S2 2026", "name": "Now", "year": "2026", "session": "S2", "status": "active"}]
    edsync.upsert_courses(e, courses)
    assert [c["id"] for c in edsync.auto_track(e, courses, m)] == [8]           # 只用 Ed：照原来跟踪 active 的
    m.execute("INSERT INTO courses (id,code,fullname,tracked) VALUES (1,'FIT1045','Sample',1)")
    assert [c["id"] for c in edsync.auto_track(e, courses, m)] == [7]           # 选了 Moodle 课以后，归档的旧课也匹配上
    m.close()
    e.close()
