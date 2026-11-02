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
