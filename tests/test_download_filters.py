"""按设置不下的文件：录像、太大的、排除的类型。合成数据，不访问真实接口。"""
import io
import zipfile

import pytest

from monash_study_kit import moodlelib, syncer
from test_moodle_content import BASE, FakeMoodle, cm, run

MB = 1024 * 1024


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(syncer, "FILES_DIR", tmp_path / "files")
    con = moodlelib.db_connect(tmp_path / "m.db")
    yield con, tmp_path / "files"
    con.close()


def _notes(con):
    return {r["url"]: r["reason"] for r in con.execute("SELECT url, reason FROM notes WHERE kind='skipped'")}


def _links(con):
    return {r["url"]: r["title"] for r in con.execute("SELECT url, title FROM links")}


def _resource(client_url, n=1, name="Lecture"):
    c = cm("resource", n, name)
    return c, {c["url"] + "&redirect=1": client_url}


def test_video_without_extension_is_caught_by_content_type(env):
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/recording"
    c, pages = _resource(file_url, name="Week 3 recording")
    client = FakeMoodle([c], pages, {file_url: b"x" * 10}, {file_url: {"Content-Type": "video/mp4"}})
    s = run(env, client)
    assert s.stats.downloaded == 0
    assert not list(env[1].rglob("recording*"))
    assert _links(env[0])[file_url].startswith("🎬")
    assert "录像" in _notes(env[0])[file_url]


def test_videos_can_be_turned_on_for_one_run(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_DOWNLOAD_VIDEOS", "true")
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/lecture.mp4"
    c, pages = _resource(file_url)
    s = run(env, FakeMoodle([c], pages, {file_url: b"video"}))
    assert s.stats.downloaded == 1 and next(env[1].rglob("lecture.mp4")).is_file()


def test_large_file_becomes_a_link(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/dataset.csv"
    c, pages = _resource(file_url, name="Dataset")
    client = FakeMoodle([c], pages, {file_url: b"x"}, {file_url: {"Content-Length": str(5 * MB)}})
    s = run(env, client)
    assert s.stats.too_large == 1 and s.stats.downloaded == 0
    assert "太大" in _links(env[0])[file_url]
    assert "max_file_mb" in _notes(env[0])[file_url]


def test_size_cap_also_works_without_content_length(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/big.bin"
    c, pages = _resource(file_url)
    s = run(env, FakeMoodle([c], pages, {file_url: b"x" * (2 * MB)}))
    assert s.stats.too_large == 1
    assert not [p for p in env[1].rglob("*") if p.is_file()]


def test_zero_means_no_limit(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "0")
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/big.pdf"
    c, pages = _resource(file_url)
    client = FakeMoodle([c], pages, {file_url: b"%PDF"}, {file_url: {"Content-Length": str(900 * MB)}})
    assert run(env, client).stats.downloaded == 1


def test_skip_types_uses_the_real_file_name(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_SKIP_TYPES", "archives, audio")
    zip_url = BASE + "/pluginfile.php/1/mod_resource/content/1/starter"          # 地址里看不出是 zip
    pdf_url = BASE + "/pluginfile.php/2/mod_resource/content/1/spec.pdf"
    c1, p1 = _resource(zip_url, 1, "Starter code")
    c2, p2 = _resource(pdf_url, 2, "Spec")
    client = FakeMoodle([c1, c2], {**p1, **p2}, {zip_url: b"PK", pdf_url: b"%PDF"},
                        {zip_url: {"Content-Disposition": 'attachment; filename="starter.zip"'}})
    s = run(env, client)
    assert s.stats.skipped_types == 1 and s.stats.downloaded == 1
    assert "archives" in _notes(env[0])[zip_url]
    assert next(env[1].rglob("spec.pdf")).is_file()


def test_folder_zip_members_follow_the_same_rules(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    c = cm("folder", name="Week 2 files")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:   # 压缩后整包很小，但里面单个文件超上限
        z.writestr("notes.pdf", b"%PDF")
        z.writestr("tutorial.mp4", b"v")
        z.writestr("data/huge.csv", b"x" * (2 * MB))
    url = BASE + "/mod/folder/download_folder.php?id=1"
    s = run(env, FakeMoodle([c], data={url: out.getvalue()}))
    assert next(env[1].rglob("notes.pdf")).is_file()
    assert not list(env[1].rglob("tutorial.mp4")) and not list(env[1].rglob("huge.csv"))
    assert s.stats.too_large == 1
    reasons = _notes(env[0])
    assert any("录像" in r for r in reasons.values()) and any("max_file_mb" in r for r in reasons.values())
    assert "2 个文件" in _links(env[0])[c["url"]]


def test_folder_member_already_downloaded_is_kept(env, monkeypatch):
    c = cm("folder", name="Recordings")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("old.mp4", b"v")
    url = BASE + "/mod/folder/download_folder.php?id=1"
    monkeypatch.setenv("MONASH_KIT_DOWNLOAD_VIDEOS", "true")
    run(env, FakeMoodle([c], data={url: out.getvalue()}))
    monkeypatch.setenv("MONASH_KIT_DOWNLOAD_VIDEOS", "false")
    env[0].execute("UPDATE files SET synced_at='2000-01-01T00:00:00+00:00'")    # 过了七天，要重下 zip
    run(env, FakeMoodle([c], data={url: out.getvalue()}))
    assert moodlelib.removed_paths(env[0]) == set()
    assert next(env[1].rglob("old.mp4")).is_file()


def test_folder_notes_survive_the_seven_day_cache(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    c = cm("folder", name="Data")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("small.txt", b"a")
        z.writestr("huge.csv", b"x" * (2 * MB))
    url = BASE + "/mod/folder/download_folder.php?id=1"
    client = FakeMoodle([c], data={url: out.getvalue()})
    run(env, client)
    run(env, client)
    assert client.requests.count(url) == 1
    assert len(_notes(env[0])) == 1 and c["url"] in _links(env[0])


def test_oversized_folder_zip_falls_back_to_single_files(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    c = cm("folder")
    small = BASE + "/pluginfile.php/1/mod_folder/content/7/a.pdf"
    zip_url = BASE + "/mod/folder/download_folder.php?id=1"
    client = FakeMoodle([c], {c["url"]: f'<a href="{small}">a</a>'},
                        {zip_url: b"PK", small: b"%PDF"}, {zip_url: {"Content-Length": str(50 * MB)}})
    run(env, client)
    assert next(env[1].rglob("a.pdf")).is_file()


def test_file_kind_and_skip_types():
    assert syncer.file_kind("Lecture.PPTX") == "slides"
    assert syncer.file_kind("noext", "video/quicktime") == "video"
    assert syncer.file_kind("noext", "application/octet-stream") is None
    assert syncer.parse_skip_types(" Audio ,archives,") == {"audio", "archives"}
    with pytest.raises(ValueError, match="可选"):
        syncer.parse_skip_types("music")


def test_stream_cap_stops_without_leaving_a_part_file(tmp_path, monkeypatch):
    from test_security import FakeResp, _fake_opener
    _fake_opener(monkeypatch, [FakeResp(200, body=b"x" * 300_000)])
    client = moodlelib.MoodleClient(jar={}, persist=False)
    with pytest.raises(moodlelib.DownloadSkipped):
        client.download(BASE + "/pluginfile.php/1/a.bin", tmp_path / "a.bin", max_bytes=100_000)
    assert list(tmp_path.iterdir()) == []


def test_sync_flags_only_change_this_run(monkeypatch):
    from types import SimpleNamespace

    from monash_study_kit import cli
    for key in ("DOWNLOAD_VIDEOS", "MAX_FILE_MB", "SKIP_TYPES"):
        monkeypatch.setenv("MONASH_KIT_" + key, "")     # 让 monkeypatch 记住原状，测完把 _sync_overrides 写的也还原
    cli._sync_overrides(SimpleNamespace(videos=True, max_mb=50.0, skip_types="audio,Images"))
    s = syncer.CourseSync(None, None, {"id": 1, "fullname": "FIT1000 Sample"}, log=lambda _: None)
    assert s.videos and s.max_bytes == 50 * MB and s.skip_types == {"audio", "images"}
    with pytest.raises(ValueError):
        cli._sync_overrides(SimpleNamespace(videos=None, max_mb=-1, skip_types=None))


def test_config_checks_new_settings():
    from monash_study_kit.cli import parse_setting
    assert parse_setting("skip_types", "audio, archives", "") == "archives,audio"
    assert parse_setting("max_file_mb", "0", 300) == 0
    for key, raw in (("skip_types", "movies"), ("max_file_mb", "-5")):
        with pytest.raises(ValueError):
            parse_setting(key, raw, 300 if key == "max_file_mb" else "")


def test_sync_help_lists_the_new_flags(capsys):
    from monash_study_kit import cli
    with pytest.raises(SystemExit):
        cli.main(["sync", "--help"])
    out = capsys.readouterr().out
    assert "--no-videos" in out and "--max-mb" in out and "--skip-types" in out


# ---------------------------------------------------------------- 跳过清单和补下

def _two_big_files(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    small = BASE + "/pluginfile.php/1/mod_resource/content/1/notes.pdf"
    big1 = BASE + "/pluginfile.php/2/mod_resource/content/1/dataset.csv"
    big2 = BASE + "/pluginfile.php/3/mod_resource/content/1/archive.zip"
    cms, pages = [], {}
    for n, (url, name) in enumerate([(small, "Notes"), (big1, "Dataset"), (big2, "Archive")], start=1):
        c, p = _resource(url, n, name)
        cms.append(c)
        pages.update(p)
    data = {small: b"%PDF", big1: b"x" * (2 * MB), big2: b"x" * (3 * MB)}
    env[0].execute("INSERT INTO courses (id, code, fullname) VALUES (1, 'FIT1000', 'FIT1000 Sample')")
    return FakeMoodle(cms, pages, data), (small, big1, big2)


def test_skipped_list_has_stable_ids_and_new_items_once(env, monkeypatch):
    from monash_study_kit import skipped as SK
    client, (_, big1, big2) = _two_big_files(env, monkeypatch)
    s = run(env, client)
    assert sorted(i["name"] for i in s.new_skips) == ["archive.zip", "dataset.csv"]
    first = {r["name"]: r["id"] for r in SK.list_skipped(env[0])}
    s = run(env, client)
    assert s.new_skips == []                                      # 第二次同步不再当成新跳过的
    assert {r["name"]: r["id"] for r in SK.list_skipped(env[0])} == first
    row = next(r for r in SK.list_skipped(env[0]) if r["name"] == "dataset.csv")
    assert row["course"] == "FIT1000" and row["why"] == "太大" and row["size"] == "2 MB"


def test_get_one_skipped_file_ignores_the_limit(env, monkeypatch):
    from monash_study_kit import skipped as SK
    client, (_, big1, big2) = _two_big_files(env, monkeypatch)
    run(env, client)
    rows = SK.list_skipped(env[0])
    before = len(client.requests)
    res = SK.fetch(client, env[0], SK.pick(rows, ["dataset"]), log=lambda _: None)
    assert [d["path"].rsplit("/", 1)[-1] for d in res["downloaded"]] == ["dataset.csv"] and not res["failed"]
    assert [r["name"] for r in SK.list_skipped(env[0])] == ["archive.zip"]
    assert big1 not in {r["url"] for r in env[0].execute("SELECT url FROM links")}
    assert big2 not in client.requests[before:]                   # 没点名的不去下
    # 之后的普通同步：上限还是 1 MB，补下的文件留着，另一个照样跳过
    s = run(env, client)
    assert next(env[1].rglob("dataset.csv")).is_file() and s.stats.too_large == 1


def test_raising_the_limit_downloads_only_what_was_skipped(env, monkeypatch):
    from monash_study_kit import skipped as SK
    client, (small, big1, big2) = _two_big_files(env, monkeypatch)
    run(env, client)
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "0")
    before = len(client.requests)
    s = run(env, client)
    fetched = [u for u in client.requests[before:] if "/pluginfile.php/" in u]
    assert sorted(fetched) == sorted([big1, big2]) and s.stats.downloaded == 2
    assert SK.list_skipped(env[0]) == []


def test_pick_rejects_unknown_and_accepts_all(env, monkeypatch):
    from monash_study_kit import skipped as SK
    client, _ = _two_big_files(env, monkeypatch)
    run(env, client)
    rows = SK.list_skipped(env[0])
    assert len(SK.pick(rows, ["all"])) == 2
    assert len(SK.pick(rows, [str(rows[0]["id"])])) == 1
    with pytest.raises(LookupError):
        SK.pick(rows, ["nothing-like-this"])


def _folder_with_big_member():
    c = cm("folder", name="Data")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("small.txt", b"a")
        z.writestr("big/huge.csv", b"x" * (2 * MB))
    return c, BASE + "/mod/folder/download_folder.php?id=1", out.getvalue()


def test_raising_the_limit_redownloads_a_cached_folder(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    c, url, data = _folder_with_big_member()
    client = FakeMoodle([c], data={url: data})
    run(env, client)
    run(env, client)
    assert client.requests.count(url) == 1                        # 七天内不重下
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "5")
    run(env, client)
    assert client.requests.count(url) == 2 and next(env[1].rglob("huge.csv")).is_file()


def test_get_a_file_from_inside_a_folder_zip(env, monkeypatch):
    from monash_study_kit import skipped as SK
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    c, url, data = _folder_with_big_member()
    client = FakeMoodle([c], data={url: data})
    env[0].execute("INSERT INTO courses (id, code, fullname) VALUES (1, 'FIT1000', 'FIT1000 Sample')")
    run(env, client)
    rows = SK.list_skipped(env[0])
    assert [r["path"] for r in rows] == ["Week 01/Data/big/huge.csv"]
    small = next(env[1].rglob("small.txt"))
    small.write_text("my notes")                                  # 自己改过的文件，补下时别覆盖
    res = SK.fetch(client, env[0], rows, log=lambda _: None)
    assert not res["failed"] and next(env[1].rglob("huge.csv")).is_file()
    assert small.read_text() == "my notes"
    assert SK.list_skipped(env[0]) == [] and c["url"] not in {r["url"] for r in env[0].execute("SELECT url FROM links")}


def test_cli_lists_skipped_files_with_hints(env, monkeypatch, capsys):
    from types import SimpleNamespace

    from monash_study_kit import cli
    client, _ = _two_big_files(env, monkeypatch)
    run(env, client)
    monkeypatch.setattr(cli, "db_connect", lambda: _Keep(env[0]))
    assert cli.cmd_skipped(SimpleNamespace(course=None, get=None, videos=False, json=False, text=True)) == 0
    out = capsys.readouterr().out
    assert "dataset.csv" in out and "太大" in out and "monash skipped --get" in out and "--max-mb" in out


class _Keep:
    """cmd_skipped 用完会 close，测试里的连接还要接着用。"""

    def __init__(self, con):
        self._con = con

    def __getattr__(self, name):
        return getattr(self._con, name)

    def close(self):
        pass


def test_sync_summary_names_new_skips(capsys):
    from monash_study_kit import cli
    cli._print_held_back({"too_large": 1, "new_skips": [{"name": "huge.csv", "size": 2 * MB, "reason": "size",
                                                         "folder": "Week 2"}]})
    out = capsys.readouterr().out
    assert "Week 2/huge.csv" in out and "monash skipped" in out


def test_list_files_can_show_skipped(env, monkeypatch):
    from monash_study_kit import mcp_server as M
    client, _ = _two_big_files(env, monkeypatch)
    run(env, client)
    monkeypatch.setattr(M, "db_connect", lambda: env[0])
    out = M.t_list_files({"skipped": True})
    assert {r["path"].rsplit("/", 1)[-1] for r in out["skipped"]} == {"dataset.csv", "archive.zip"}
    assert "monash skipped --get" in out["how_to_get"]


def test_known_skip_is_not_requested_again(env, monkeypatch):
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "1")
    file_url = BASE + "/pluginfile.php/1/mod_resource/content/1/recording"    # 只有响应头说得出是什么
    c, pages = _resource(file_url)
    client = FakeMoodle([c], pages, {file_url: b"x" * (2 * MB)}, {file_url: {"Content-Type": "video/mp4"}})
    run(env, client)
    run(env, client)
    assert [u for u in client.requests if u == file_url] == [file_url]      # 第二次同步不再去碰它
    monkeypatch.setenv("MONASH_KIT_DOWNLOAD_VIDEOS", "true")
    monkeypatch.setenv("MONASH_KIT_MAX_FILE_MB", "0")
    run(env, client)
    assert next(env[1].rglob("recording*")).is_file()


def test_windows_reserved_names_get_a_prefix():
    assert syncer.windows_safe("CON.pdf", windows=True) == "_CON.pdf"
    assert syncer.windows_safe("nul", windows=True) == "_nul"
    assert syncer.windows_safe("console.pdf", windows=True) == "console.pdf"
    assert syncer.windows_safe("CON.pdf", windows=False) == "CON.pdf"


def test_get_refuses_when_disk_is_too_small(monkeypatch, tmp_path):
    import shutil
    from collections import namedtuple
    from monash_study_kit import skipped as SK
    monkeypatch.setattr(syncer, "FILES_DIR", tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(10 * MB * 1024, 0, 600 * MB))
    with pytest.raises(ValueError, match="硬盘"):
        SK.check_space([{"bytes": 200 * MB}])
    SK.check_space([{"bytes": 50 * MB}])


def test_partial_syncs_wait_for_the_background_sync(monkeypatch, tmp_path):
    from monash_study_kit import jobs
    monkeypatch.setattr(jobs, "LOCK_FILE", tmp_path / "sync.lock")
    assert jobs._lock()
    with pytest.raises(jobs.SyncBusy):
        with jobs.sync_lock():
            pass
    jobs._unlock()
    with jobs.sync_lock():
        assert (tmp_path / "sync.lock").exists()
    assert not (tmp_path / "sync.lock").exists()


def test_ai_can_ask_for_skipped_files(env, monkeypatch):
    from monash_study_kit import jobs, mcp_server as M, skipped as SK
    client, _ = _two_big_files(env, monkeypatch)
    run(env, client)
    monkeypatch.setattr(M, "db_connect", lambda: _Keep(env[0]))
    monkeypatch.setattr(M, "MoodleClient", lambda: client)
    monkeypatch.setattr(jobs, "LOCK_FILE", env[1].parent / "sync.lock")
    got = []
    monkeypatch.setattr(SK, "fetch", lambda cl, con, picked, log=None: got.extend(picked) or {"downloaded": [], "failed": []})
    msg = M.t_sync({"get_skipped": "dataset"})
    M._sync_thread.join(5)
    assert "1 个文件" in msg and [r["name"] for r in got] == ["dataset.csv"]


def test_untracked_units_are_left_out_of_the_list(env, monkeypatch):
    from monash_study_kit import skipped as SK
    client, _ = _two_big_files(env, monkeypatch)
    run(env, client)
    env[0].execute("INSERT INTO courses (id, code, fullname, tracked) VALUES (2, 'FIT2000', 'New unit', 1)")
    assert SK.list_skipped(env[0]) == []                       # FIT1000 已经不跟踪了
    assert len(SK.list_skipped(env[0], 1)) == 2                # 点名要看还是能看
