"""monash export：Moodle 和 Ed Lessons 合成一份课程资料，索引写明没导出的内容和原因。

库和文件都是合成的，不联网。"""
import io
import json
import zipfile

import pytest

from monash_study_kit import cli, edexport, edlib, export as X, moodlelib
from monash_study_kit import lessons as L


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "load_settings", lambda: {"export_format": "md"})
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
    # 读进内存再打开：Windows 上打开着的 zip 不能被下一次导出覆盖
    return d, zipfile.ZipFile(io.BytesIO(open(d["path"], "rb").read()))


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


def test_google_sync_hides_success_and_caches(env, monkeypatch):
    from monash_study_kit import google_export as G, syncer
    _moodle_course(env)
    monkeypatch.setattr(syncer, "FILES_DIR", env["files"])
    calls=[]
    monkeypatch.setattr(G, "download", lambda url, client=None: calls.append(url) or ("Sample.pdf", b"pdf"))
    course={"id":7,"fullname":"FIT1043 Introduction to data science (S2 2025)"}
    # 测试使用实际数据库中的课程目录，避免标题格式影响文件位置。
    monkeypatch.setattr(syncer,"course_folder",lambda _: env["m"].execute("SELECT folder FROM courses WHERE id=7").fetchone()[0])
    G.sync_course(env["m"],course,log=lambda _:None)
    syncer.write_links_md(env["m"],7,syncer.course_folder(course))
    local=next(env["files"].rglob("links.md"))
    assert "docs.google.com" not in local.read_text(encoding="utf-8")
    _,z=_zip(env)
    assert "docs.google.com" not in z.read("FIT1043/Moodle/Week 01 - Intro/links.md").decode()
    G.sync_course(env["m"],course,log=lambda _:None)
    assert len(calls) == 1
    G.sync_course(env["m"],course,refresh=True,log=lambda _:None)
    assert len(calls) == 2
    result,_=_zip(env)
    assert result["statistics"]["google_success"] == 1


def test_google_failure_notes_and_statistics(env,monkeypatch):
    from monash_study_kit import google_export as G, syncer
    _moodle_course(env)
    _ed_course(env)
    monkeypatch.setattr(syncer,"FILES_DIR",env["files"])
    def failed(*a):
        raise G.GoogleError("还没授权 Google（monash google login）")
    monkeypatch.setattr(G,"download",failed)
    G.sync_course(env["m"],{"id":7,"fullname":"FIT1043 Sample"},log=lambda _:None)
    env["m"].execute("INSERT INTO notes VALUES (7,'grading_hidden','Week 1','Sample brief','https://example.com/brief','尚未显示评分细则','x')")
    md=env["files"]/"remote.md"
    md.write_text("![sample](https://example.com/image.png)", encoding="utf-8")
    env["m"].execute("INSERT INTO files (course_id,source,path,synced_at) VALUES (7,'synthetic', 'remote.md','x')")
    result,z=_zip(env)
    index=z.read("FIT1043/README.md").decode()
    assert "尚未显示评分细则" in index and "monash google login" in index and "## 统计" in index
    stats=result["statistics"]
    assert stats["link_files"] == 2 and stats["remote_images"] == 1
    assert stats["google_total"] == stats["google_failed"] == 1 and stats["google_success"] == 0
    assert stats["google_failure_reasons"] == {"还没授权 Google（monash google login）":1}


def test_ed_google_webpage_exported_in_lesson_directory(env,monkeypatch):
    from monash_study_kit import google_export as G, syncer
    _moodle_course(env)
    _ed_course(env)
    monkeypatch.setattr(syncer,"FILES_DIR",env["files"])
    monkeypatch.setattr(L,"FILES_DIR",env["ed_files"])
    url="https://docs.google.com/document/d/ed-sample/edit"
    env["e"].execute("INSERT INTO lesson_files (id,lesson_id,course_id,type,title,url,idx) VALUES (104,11,24100,'webpage','Google brief',?,3)",(url,))
    monkeypatch.setattr(G,"download",lambda *a:("Brief.pdf",b"pdf"))
    G.sync_course(env["m"],{"id":7,"fullname":"FIT1043 Sample"},ed_conn=env["e"],log=lambda _:None)
    rel=env["e"].execute("SELECT local_path FROM lesson_files WHERE id=104").fetchone()[0]
    assert "/01 W1 Pre-Class/Brief.pdf" in rel and (env["ed_files"]/rel).is_file()
    _,z=_zip(env)
    arc="FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/Brief.pdf"
    assert arc in z.namelist()
    assert url not in z.read("FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/Links.md").decode()


def test_export_sync_calls_sync_before_export(env,monkeypatch):
    calls=[]
    monkeypatch.setattr(cli,"sync_export_course",lambda *a,**kw:calls.append("sync"))
    monkeypatch.setattr(moodlelib,"db_connect",lambda:env["m"])
    monkeypatch.setattr(edlib,"db_connect",lambda:env["e"])
    def export(*a,**kw):
        calls.append("export")
        return {"path":"sample.zip"}
    monkeypatch.setattr(X,"export",export)
    args=cli.build_parser().parse_args(["export","FIT1043","--sync","--json"])
    assert args.fn(args) is None
    assert calls == ["sync","export"]


def test_export_sync_orders_moodle_before_ed(env,monkeypatch):
    from monash_study_kit import jobs, google_export as G
    calls=[]
    course={"id":7,"fullname":"FIT1043 Sample"}
    monkeypatch.setattr(cli,"MoodleClient",lambda:object())
    monkeypatch.setattr(cli,"db_connect",lambda:env["m"])
    monkeypatch.setattr(jobs,"load_courses",lambda *a:[course])
    monkeypatch.setattr(jobs,"sync_moodle",lambda **kw:calls.append(("moodle",kw["only"])))
    monkeypatch.setattr(jobs,"sync_ed",lambda **kw:calls.append(("ed",kw["course_code"])))
    monkeypatch.setattr(jobs,"have_ed_token",lambda:True)
    monkeypatch.setattr(edlib,"db_connect",lambda:env["e"])
    monkeypatch.setattr(G,"sync_course",lambda *a,**kw:calls.append(("google",7)))
    monkeypatch.setattr(cli,"write_links_md",lambda *a:None)
    cli.sync_export_course("FIT1043",log=lambda _:None)
    assert calls == [("moodle",[course]),("ed","FIT1043"),("google",7)]


def test_quiz_images_point_at_local_assets(env):
    """测验题里的 Ed 图片：同步时下到 assets/，导出的 Quiz.md 里换成相对路径。"""
    _moodle_course(env)
    _ed_course(env)
    url = "https://static.au.edusercontent.com/files/QUIZIMG"
    env["e"].execute("UPDATE quiz_questions SET question=? WHERE id=1", (f"Which gate is this?\n\n![图片]({url})",))
    asset = env["ed_files"] / "FIT1043" / "assets" / "abcd1234abcd1234.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    env["e"].execute("INSERT INTO ed_assets VALUES (?,?,?,NULL,'x')", (url, "FIT1043/assets/abcd1234abcd1234.png", "s"))
    env["e"].commit()
    from monash_study_kit import lessons as L2
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(L2, "FILES_DIR", env["ed_files"])
    try:
        _, z = _zip(env)
    finally:
        mp.undo()
    quiz = z.read("FIT1043/Ed Lessons/01 Week 1 - Intro/01 W1 Pre-Class/Quiz.md").decode()
    assert url not in quiz and "](../../assets/abcd1234abcd1234.png)" in quiz
    assert "FIT1043/Ed Lessons/assets/abcd1234abcd1234.png" in z.namelist()
