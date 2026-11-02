"""monash export：Moodle 和 Ed Lessons 合成一份课程资料，索引写明没导出的内容和原因。

库和文件都是合成的，不联网。"""
import json
import zipfile

import pytest

from monash_study_kit import cli, edexport, edlib, export as X, moodlelib
from monash_study_kit import lessons as L


@pytest.fixture
def env(tmp_path, monkeypatch):
    files, ed_files = tmp_path / "files", tmp_path / "ed-files"
    monkeypatch.setattr(X, "FILES_DIR", files)
    monkeypatch.setattr(edexport, "ED_FILES_DIR", ed_files)
    mcon = moodlelib.db_connect(tmp_path / "moodle.db")
    econ = edlib.db_connect(tmp_path / "ed.db")
    L.ensure_lessons_schema(econ)
    return {"tmp": tmp_path, "files": files, "ed_files": ed_files, "m": mcon, "e": econ}


def _moodle_course(env, code="FIT1043", cid=7):
    root = f"{code} Introduction to data science (S2 2025)"
    env["m"].execute("INSERT INTO courses (id, code, folder, fullname, tracked, synced_at) VALUES (?,?,?,?,1,'x')",
                     (cid, code, root, root))
    pdf = env["files"] / root / "Week 01 - Intro" / "Lecture 1.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 lecture")
    env["m"].execute("INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (cid, "https://learning.monash.edu/pluginfile.php/1/x/Lecture%201.pdf",
                                                   1, f"{root}/Week 01 - Intro/Lecture 1.pdf", 16, "Week 1", "Lecture 1", "x"))
    env["m"].execute("INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (cid, "https://learning.monash.edu/pluginfile.php/2/x/gone.pdf",
                                                   2, f"{root}/Week 01 - Intro/gone.pdf", 1, "Week 1", "Gone", "x"))
    for title, url in (("Slides", "https://docs.google.com/presentation/d/abc/edit"),
                       ("Zoom Link", "https://monash.zoom.us/j/88358088027?pwd=SECRETPWD&from=addon")):
        env["m"].execute("INSERT INTO links (course_id, folder, section, title, url, synced_at) VALUES (?,?,?,?,?,'x')",
                         (cid, "Week 01 - Intro", "Week 1", title, url))
    env["m"].commit()


def _ed_course(env, code="FIT1043 S2 2025", cid=24100, tracked=1, lessons=True):
    env["e"].execute("INSERT INTO courses (id, code, name, year, session, status, tracked) VALUES (?,?,?,?,?,?,?)",
                     (cid, code, "Data science", "2025", "S2", "archived", tracked))
    if lessons:
        env["e"].execute("INSERT INTO modules (id, course_id, name) VALUES (1, ?, 'Week 1: Intro')", (cid,))
        env["e"].execute("INSERT INTO lessons (id, course_id, module_id, idx, title, status) "
                         "VALUES (11, ?, 1, 0, 'W1 Pre-Class', 'completed')", (cid,))
        rel = "FIT1043/01 Week 1 - Intro/01 W1 Pre-Class/01 Slides.pdf"
        (env["ed_files"] / rel).parent.mkdir(parents=True)
        (env["ed_files"] / rel).write_bytes(b"%PDF ed")
        env["e"].execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title, file_url, url, local_path, idx) "
                         "VALUES (101, 11, ?, 'pdf', 'Slides', 'https://static.edusercontent.com/files/A', NULL, ?, 0)",
                         (cid, rel))
        env["e"].execute("INSERT INTO lesson_files (id, lesson_id, course_id, type, title, file_url, url, local_path, idx) "
                         "VALUES (102, 11, ?, 'pdf', 'Big data', 'https://static.edusercontent.com/files/B', NULL, NULL, 1)",
                         (cid,))
        env["e"].execute("INSERT INTO quiz_questions VALUES (1, 103, 11, ?, 'Check', 0, 'multiple-choice', 0, "
                         "'What is 1+1?', ?, 'x')", (cid, json.dumps(["1", "2"])))
    env["e"].commit()


def _zip(env, **kw):
    d = X.export("FIT1043", env["tmp"] / "out", moodle_con=env["m"], ed_conn=env["e"], **kw)
    return d, zipfile.ZipFile(d["path"])


def test_moodle_and_ed_in_one_tree(env):
    _moodle_course(env)
    _ed_course(env)
    d, z = _zip(env)
    names = set(z.namelist())
    assert "FIT1043/Moodle/Week 01 - Intro/Lecture 1.pdf" in names
    assert "FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/01 Slides.pdf" in names
    assert "FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/Quiz.md" in names
    assert "What is 1+1?" in z.read("FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/Quiz.md").decode()
    assert d["moodle"]["files"] == 1 and d["ed"]["lessons"] == 1


def test_index_lists_missing_with_reasons(env):
    _moodle_course(env)
    _ed_course(env)
    _, z = _zip(env)
    index = z.read("FIT1043/README.md").decode()
    assert "## 没导出的内容" in index
    assert "Gone" in index and "本地文件不见了" in index
    assert "Big data" in index and "没下载到" in index


def test_meeting_passwords_are_scrubbed(env):
    _moodle_course(env)
    _, z = _zip(env)
    links = z.read("FIT1043/Moodle/Week 01 - Intro/links.md").decode()
    assert "SECRETPWD" not in links
    assert "https://monash.zoom.us/j/88358088027?from=addon" in links
    assert "docs.google.com/presentation/d/abc" in links


def test_untracked_ed_course_explains_why_lessons_are_empty(env):
    """FIT1045 的情况：Moodle 只有链接，Ed 课没跟踪所以 lessons 一节都没同步。"""
    _moodle_course(env)
    _ed_course(env, tracked=0, lessons=False)
    _, z = _zip(env)
    index = z.read("FIT1043/README.md").decode()
    assert "没有被跟踪" in index
    assert not any(n.startswith("FIT1043/Ed Lessons/") for n in z.namelist())


def test_ed_course_missing_from_local_list(env):
    _moodle_course(env)
    _, z = _zip(env)
    assert "Ed 课程列表里没有 FIT1043" in z.read("FIT1043/README.md").decode()


def test_nothing_to_export_is_an_error(env):
    with pytest.raises(LookupError, match="没有可导出的资料"):
        X.export("FIT9999", env["tmp"] / "out", moodle_con=env["m"], ed_conn=env["e"])


def test_export_as_folder(env):
    _moodle_course(env)
    d = X.export("fit1043", env["tmp"] / "out", as_dir=True, moodle_con=env["m"], ed_conn=env["e"])
    assert (env["tmp"] / "out" / "FIT1043" / "Moodle" / "Week 01 - Intro" / "Lecture 1.pdf").is_file()
    assert d["path"].endswith("FIT1043")


def test_scrub_url_keeps_other_params():
    assert X.scrub_url("https://x.zoom.us/j/1?pwd=a&tk=b&uname=c") == "https://x.zoom.us/j/1?uname=c"
    assert X.scrub_url("https://example.com/a") == "https://example.com/a"


def test_cli_export(env, monkeypatch, capsys):
    _moodle_course(env)
    _ed_course(env)
    monkeypatch.setattr(moodlelib, "db_connect", lambda *a, **k: env["m"])
    monkeypatch.setattr(edlib, "db_connect", lambda *a, **k: env["e"])
    assert cli.main(["export", "FIT1043", "--to", str(env["tmp"] / "cli"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["course"] == "FIT1043" and out["path"].endswith(".zip")
