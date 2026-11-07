"""命名账号和课程资料模式；所有数据均为临时合成文件，所有接口均使用假客户端。"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from monash_study_kit import cli, cli_profile as P, edlib, edsync, google_export as G, jobs, moodlelib, paths
from monash_study_kit import mcp_server as M
from monash_study_kit import materials_policy as policy

ROOT = Path(__file__).resolve().parents[1]


def child(tmp_path, args, script=None, extra=None):
    env = {k:v for k,v in os.environ.items() if not k.startswith(("MONASH_KIT_", "_MONASH_KIT_")) and k != "ED_TOKEN"}
    # 子进程的报错里有中文：Windows 的管道默认不是 UTF-8
    env.update(MONASH_KIT_HOME=str(tmp_path/"home"), MONASH_KIT_UPDATE_CHECK="false", PYTHONIOENCODING="utf-8")
    env.update(extra or {})
    script = script or """
import json, sys
from monash_study_kit.entry import configure
rest = configure(sys.argv[1:])
from monash_study_kit import paths as p
print(json.dumps({k:str(getattr(p,k)) for k in ('HOME','SECRETS_DIR','DATA_DIR','FILES_DIR','ED_DB','ED_FILES_DIR','BROWSER_PROFILE','ED_TOKEN_FILE')} | {'profile':p.PROFILE,'materials':p.load_settings()['materials_only'],'rest':rest,'default':p.is_default_profile()}))
"""
    return subprocess.run([sys.executable,"-c",script,*args],cwd=ROOT,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=20)


@pytest.mark.parametrize("flag", [["--profile","alice"], ["--profile=alice"], []])
def test_profile_paths_initialized_before_cli(tmp_path,flag):
    extra={"MONASH_KIT_PROFILE":"alice"} if not flag else {}
    d=json.loads(child(tmp_path,flag,extra=extra).stdout)
    default=json.loads(child(tmp_path,[]).stdout)
    assert d["profile"] == "alice" and d["materials"] is True and not d["default"]
    for key in ("HOME","SECRETS_DIR","DATA_DIR","FILES_DIR","ED_DB","ED_FILES_DIR","BROWSER_PROFILE","ED_TOKEN_FILE"):
        assert d[key] != default[key]
        assert Path(d[key]).is_relative_to(tmp_path/"home/profiles/alice")
    if os.name != "nt":
        assert (tmp_path/"home/profiles/alice").stat().st_mode & 0o777 == 0o700


def test_default_profile_changes_nothing(tmp_path):
    a=json.loads(child(tmp_path,[]).stdout)
    b=json.loads(child(tmp_path,["--profile","default"]).stdout)
    assert a == b and a["profile"] is None and a["materials"] is False
    assert a["HOME"] == str(tmp_path/"home") and a["SECRETS_DIR"] == str(tmp_path/"home/secrets")


@pytest.mark.parametrize("name",["../other","Alice","a/b","a.b","-alice","a"*33,""])
def test_invalid_profile_names_do_not_initialize_cli(tmp_path,name):
    result=child(tmp_path,["--profile="+name])
    assert result.returncode != 0 and "profile 名称" in result.stderr
    assert not (tmp_path/"home/profiles").exists()


def test_default_is_reserved_for_management():
    from monash_study_kit.home import validate_profile
    with pytest.raises(ValueError,match="保留名"):
        validate_profile("default")


def test_profile_ignores_shared_directory_overrides_and_cannot_disable_materials(tmp_path):
    extra={key:str(tmp_path/"shared") for key in ("MONASH_KIT_DATA","MONASH_KIT_SECRETS","MONASH_KIT_FILES","MONASH_KIT_ED_DATA","MONASH_KIT_ED_TOKEN")}
    extra["MONASH_KIT_MATERIALS_ONLY"]="false"
    d=json.loads(child(tmp_path,["--profile","alice"],extra=extra).stdout)
    assert d["materials"] is True
    assert all(not Path(d[key]).is_relative_to(tmp_path/"shared") for key in ("HOME","SECRETS_DIR","DATA_DIR","FILES_DIR","ED_DB","ED_FILES_DIR","ED_TOKEN_FILE"))


def test_profile_inherited_by_child_does_not_nest_again(tmp_path):
    script="""
import os, subprocess, sys
from monash_study_kit.entry import configure
configure(['--profile','alice'])
r=subprocess.run([sys.executable,'-m','monash_study_kit','config','materials_only'],capture_output=True,text=True,encoding="utf-8",errors="replace")
print(os.environ['MONASH_KIT_HOME'])
print(r.stdout)
assert r.returncode == 0
"""
    result=child(tmp_path,[],script)
    assert result.returncode == 0 and "profiles/alice/profiles" not in result.stdout and "true" in result.stdout.lower()


def test_entry_cli_default_override_of_env(tmp_path):
    d=json.loads(child(tmp_path,["--profile=default"],extra={"MONASH_KIT_PROFILE":"alice"}).stdout)
    assert d["HOME"] == str(tmp_path/"home") and d["profile"] is None


@pytest.mark.parametrize("argv",[
    ["gmail","status"], ["attendance","status"], ["moodle","grades"], ["moodle","assignments"],
    ["moodle","messages"], ["moodle","overview"], ["moodle","calendar"], ["moodle","news"],
    ["moodle","search","sample"], ["moodle","find","sample"], ["moodle","alerts"], ["moodle","open","FIT1000"],
    ["ed","threads"], ["ed","search","sample"], ["ed","show","1"], ["ed","new"], ["ed","following"],
    ["ed","courses"], ["todo"], ["due"], ["semester"], ["media"], ["uninstall"], ["update"],
    ["connect"], ["remote","clients"], ["move","/temporary-destination"], ["setup","claude"],
])
def test_materials_mode_denies_before_handler(argv,monkeypatch,capsys):
    monkeypatch.setattr(cli,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(cli,"ensure_private_dir",lambda *a:pytest.fail("被禁命令不能初始化目录或调用服务"))
    assert cli.main(argv) == 1
    assert "只导课程资料模式下不能用：" in capsys.readouterr().err


@pytest.mark.parametrize("argv",[
    ["login"], ["login","ed"], ["google","login"], ["google","status"], ["google","logout"],
    ["sync"], ["export","FIT1000"], ["ed","export","FIT1000"], ["courses"], ["status"], ["doctor"],
    ["open"], ["library"], ["config"], ["grep","sample"], ["ed","lessons","FIT1000"],
    ["ed","read","FIT1000","1"], ["ed","quiz","FIT1000"], ["moodle","ls","FIT1000"],
    ["moodle","get","1"], ["help"], ["profile","list"], ["mcp"], ["setup"],
])
def test_materials_mode_allows_documented_commands(argv):
    args=cli.build_parser().parse_args(argv)
    assert policy.command_error(args,{"materials_only":True}) is None


@pytest.mark.parametrize("argv",[["remote","clients"],["connect"],["setup","claude"],["move","/temporary-destination"],["profile","list"],["config","materials_only","false"],["config","files_dir","elsewhere"]])
def test_named_profile_local_only_guards(argv,monkeypatch,capsys):
    monkeypatch.setattr(paths,"PROFILE","alice")
    monkeypatch.setattr(cli,"load_settings",lambda:{"materials_only":True})
    assert cli.main(argv) == 1
    assert capsys.readouterr().err


def test_mcp_tools_are_materials_only_and_forbidden_calls_fail(monkeypatch):
    monkeypatch.setattr(M,"load_settings",lambda:{"materials_only":True,"gmail_enabled":True,"gmail_manage_enabled":True,"attendance_enabled":True,"attendance_submit_enabled":True})
    assert {t["name"] for t in M.tool_list()} == policy.MCP_TOOLS
    for name in (set(M.TOOLS)-policy.MCP_TOOLS)|{"gmail_search","execute_student_action","attendance_sync","ed_threads"}:
        reply=M.handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":{}}})
        assert "error" in reply
        assert M.call_tool(name,{})["isError"]
    init=M.handle({"id":1,"method":"initialize","params":{}})["result"]["instructions"]
    assert "只导课程资料模式" in init and "get_study_todo" not in init


def test_materials_sync_never_fetches_ed_threads(tmp_path,monkeypatch):
    db=tmp_path/"ed.db"
    m=moodlelib.db_connect(tmp_path/"m.db")
    m.execute("INSERT INTO courses (id,code,tracked) VALUES (1,'FIT1000',1)")
    m.commit()
    requests=[]
    class FakeEd:
        def courses(self):
            requests.append("/courses")
            return [{"id":7,"code":"FIT1000/FIT1001 S1 2025","name":"Sample","year":"2025","session":"S1","status":"archived"}]
        def get(self,path):
            requests.append(path)
            if path=="/courses/7/lessons":
                return {"modules":[],"lessons":[{"id":9,"index":0,"title":"Sample"}]}
            if path=="/lessons/9":
                return {"lesson":{"slides":[]}}
            pytest.fail("不应请求："+path)
    real_ed=edlib.db_connect
    monkeypatch.setattr(edlib,"db_connect",lambda:real_ed(db))
    monkeypatch.setattr(edlib,"EdClient",FakeEd)
    class Shared:
        def execute(self,*a):
            return m.execute(*a)
        def close(self):
            pass
    monkeypatch.setattr(moodlelib,"db_connect",lambda:Shared())
    monkeypatch.setattr(jobs,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(edsync,"sync",lambda **kw:pytest.fail("不能同步论坛帖子"))
    result=jobs.sync_ed(log=lambda _:None)
    assert result == {"new_threads":0,"new_replies":0,"failed":0,"failures":[]}
    assert requests == ["/courses","/courses/7/lessons","/lessons/9"]
    con=real_ed(db)
    assert con.execute("SELECT title FROM lessons WHERE id=9").fetchone()[0] == "Sample"
    con.close()
    m.close()


@pytest.fixture
def profiles(tmp_path,monkeypatch):
    monkeypatch.setattr(paths,"PROFILE",None)
    monkeypatch.setattr(paths,"HOME",tmp_path)
    monkeypatch.setattr(paths,"SECRETS_DIR",tmp_path/"secrets")
    for root in (tmp_path,tmp_path/"profiles/alice",tmp_path/"profiles/bob"):
        for directory in ("secrets","data","files","browser-profile","attendance-browser"):
            (root/directory).mkdir(parents=True,exist_ok=True)
        for name in ("moodle-cookie","ed-token","google-drive-token.json","gmail-token.json"):
            (root/"secrets"/name).write_text("编造的文件内容，不是凭据", encoding="utf-8")
        (root/"data/course.db").write_text("编造的数据库", encoding="utf-8")
        (root/"files/sample.txt").write_text("编造的课件", encoding="utf-8")
    return tmp_path


def snapshot(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("all_data",[False,True])
def test_profile_remove_revokes_all_then_deletes_only_target(profiles,monkeypatch,all_data):
    alice=profiles/"profiles/alice"
    default=snapshot(profiles/"secrets")
    bob=snapshot(profiles/"profiles/bob")
    called=[]
    def revoke(path):
        assert (alice/"secrets/moodle-cookie").is_file()
        assert (alice/"browser-profile").is_dir() and (alice/"attendance-browser").is_dir()
        assert path.parent == alice/"secrets"
        called.append(path.name)
    monkeypatch.setattr(G,"revoke_token",revoke)
    monkeypatch.setattr(moodlelib,"MoodleClient",lambda *a,**kw:pytest.fail("清理不能请求 Moodle"))
    logs=[]
    assert P.remove_profile("alice",all_data=all_data,yes=True,log=logs.append)
    assert called == list(P.TOKENS)
    assert snapshot(profiles/"secrets") == default and snapshot(profiles/"profiles/bob") == bob
    assert P.ED_HINT in logs
    if all_data:
        assert not alice.exists()
    else:
        assert (alice/"data/course.db").is_file() and (alice/"files/sample.txt").is_file()
        assert not (alice/"secrets").exists() and not (alice/"browser-profile").exists()
        assert str(alice) in logs[0]


def test_profile_revoke_failure_preserves_every_local_file(profiles,monkeypatch):
    before=snapshot(profiles)
    called=[]
    def revoke(path):
        called.append(path.name)
        if path.name=="gmail-token.json":
            raise G.GoogleError("编造的吊销失败")
    monkeypatch.setattr(G,"revoke_token",revoke)
    with pytest.raises(G.GoogleError):
        P.remove_profile("alice",all_data=True,yes=True)
    assert len(called)==2 and snapshot(profiles)==before


def test_profile_remove_confirmation_lists_paths_and_can_cancel(profiles,monkeypatch):
    prompts=[]
    monkeypatch.setattr(G,"revoke_token",lambda *a:pytest.fail("未确认不能吊销"))
    before=snapshot(profiles)
    assert not P.remove_profile("alice",ask=lambda text:prompts.append(text) or "no",log=lambda _:None)
    assert str(profiles/"profiles/alice/secrets") in prompts[0]
    assert str(profiles/"profiles/alice/browser-profile") in prompts[0]
    assert snapshot(profiles)==before


def test_profile_list_does_not_read_credentials(profiles,monkeypatch,capsys):
    real=Path.read_text
    def read(path,*a,**kw):
        if "secrets" in path.parts:
            pytest.fail("列表不能读取凭据")
        return real(path,*a,**kw)
    monkeypatch.setattr(Path,"read_text",read)
    assert P.run(SimpleNamespace(profile_cmd="list"))==0
    output=capsys.readouterr().out
    assert "alice" in output and "bob" in output and "default" in output
    assert "编造的文件内容" not in output and "Moodle 有" in output
    assert all(r["bytes"] > 0 for r in P.list_profiles())


@pytest.mark.parametrize("name",["default","../bob","Alice"])
def test_remove_rejects_invalid_name_without_requests(profiles,monkeypatch,name):
    monkeypatch.setattr(G,"revoke_token",lambda *a:pytest.fail("非法名称不能吊销"))
    before=snapshot(profiles)
    with pytest.raises(ValueError):
        P.remove_profile(name,yes=True)
    assert snapshot(profiles)==before


def test_profile_remove_rejects_symlink_to_default(profiles,monkeypatch):
    link=profiles/"profiles/link"
    try:
        link.symlink_to(profiles,target_is_directory=True)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    monkeypatch.setattr(G,"revoke_token",lambda *a:pytest.fail("符号链接不能吊销"))
    with pytest.raises(ValueError,match="符号链接"):
        P.remove_profile("link",all_data=True,yes=True)


def test_background_skips_private_services_even_if_enabled(monkeypatch):
    settings={"materials_only":True,"gmail_enabled":True,"gmail_sync_minutes":30,"attendance_enabled":True}
    monkeypatch.setattr(M,"load_settings",lambda:settings)
    assert M.start_mail_sync() is False and M.start_attendance_work() is None
    from monash_study_kit import update_check
    monkeypatch.setattr(update_check,"enabled",lambda:False)
    monkeypatch.setattr(jobs,"hours_since_sync",lambda:0)
    monkeypatch.setattr(M,"start_mail_sync",lambda:pytest.fail("不能启动邮箱后台任务"))
    monkeypatch.setattr(M,"start_attendance_work",lambda:pytest.fail("不能启动考勤后台任务"))
    import time
    M._background_tick(3600,3600,time.time())


def test_materials_login_does_not_fetch_calendar(monkeypatch):
    from monash_study_kit import browser_login, features
    monkeypatch.setattr(paths,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(features,"calendar_url",lambda *a:pytest.fail("不能读个人日历"))
    browser_login._save_calendar_url(object(),lambda _:None)


@pytest.mark.parametrize("url",["https://learning.monash.edu/grade/report/user/index.php?id=1", "https://learning.monash.edu/pluginfile.php/1/assignsubmission_file/submission_files/1/private.pdf", "https://evil.example/file.pdf"])
def test_moodle_get_cannot_download_personal_pages(url):
    args=cli.build_parser().parse_args(["moodle","get",url])
    assert "只能下载课程资源" in policy.command_error(args,{"materials_only":True})


def test_mode_setting_default_can_enable_but_active_mode_cannot_change():
    args=cli.build_parser().parse_args(["config","materials_only","true"])
    assert policy.command_error(args,{"materials_only":False}) is None
    args.value="false"
    assert "只导课程资料模式下不能用" in policy.command_error(args,{"materials_only":True})
    args.value=None
    assert policy.command_error(args,{"materials_only":True}) is None


def test_profile_setup_only_logs_in_and_selects_courses(tmp_path,monkeypatch):
    from monash_study_kit import browser_login, claude_setup, location
    monkeypatch.setattr(paths,"PROFILE","alice")
    monkeypatch.setattr(cli,"HOME",tmp_path)
    monkeypatch.setattr(cli,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(jobs,"have_ed_token",lambda:True)
    monkeypatch.setattr(browser_login,"session_ok",lambda:True)
    monkeypatch.setattr(location,"first_run",lambda:pytest.fail("profile 不能选择全局数据位置"))
    monkeypatch.setattr(claude_setup,"install_code",lambda:pytest.fail("不能写 AI 配置"))
    monkeypatch.setattr(claude_setup,"install_desktop",lambda:pytest.fail("不能写 AI 配置"))
    monkeypatch.setattr(cli,"_yes",lambda *a,**kw:False)
    called=[]
    monkeypatch.setattr(cli,"choose_courses",lambda **kw:called.append("courses"))
    monkeypatch.setattr(cli,"cmd_sync",lambda *a:pytest.fail("profile setup 只做登录选课"))
    assert cli.cmd_setup(SimpleNamespace(what=None)) == 0
    assert called == ["courses"]


def test_doctor_skips_private_checks(monkeypatch):
    from monash_study_kit import doctor, doctor_deep
    called=[]
    def check(name):
        def fn(*a):
            called.append(name)
            return doctor._check(name,doctor.OK,"编造的状态")
        fn.__name__=name
        return fn
    monkeypatch.setattr(doctor,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(doctor,"CHECKS",[check(n) for n in ("check_moodle","check_gmail","check_attendance","check_claude_code","check_claude_desktop")])
    doctor.run()
    assert called == ["check_moodle"]
    called.clear()
    monkeypatch.setattr(doctor_deep,"load_settings",lambda:{"materials_only":True})
    monkeypatch.setattr(doctor_deep,"Context",lambda:SimpleNamespace(con=SimpleNamespace(close=lambda:None)))
    monkeypatch.setattr(doctor_deep,"DEEP_CHECKS",[check(n) for n in ("deep_files","deep_index","deep_due","deep_grades","deep_ed","deep_assignments")])
    doctor_deep.run()
    assert called == ["deep_files","deep_index"]


def test_default_directory_environment_overrides_are_preserved(tmp_path):
    d=json.loads(child(tmp_path,[],extra={"MONASH_KIT_SECRETS":str(tmp_path/"custom-secrets"),"MONASH_KIT_FILES":str(tmp_path/"custom-files")}).stdout)
    assert d["SECRETS_DIR"] == str(tmp_path/"custom-secrets")
    assert d["FILES_DIR"] == str(tmp_path/"custom-files")


def test_profile_uses_location_pointer_without_home_environment(tmp_path):
    script="""
import json, os, sys
from pathlib import Path
from monash_study_kit import entry, home
base=Path(sys.argv[1])
root=base/'system-default'
root.mkdir()
pointer=root/'location.json'
pointer.write_text(json.dumps({'home':str(base/'moved')}), encoding="utf-8")
os.environ.pop('MONASH_KIT_HOME',None)
entry.resolve_home=lambda environ:home.resolve_home(environ,pointer,root)
entry.configure(['--profile','alice'])
from monash_study_kit import paths
assert paths.HOME == base/'moved/profiles/alice'
print(paths.HOME)
"""
    result=child(tmp_path,[str(tmp_path)],script)
    assert result.returncode == 0, result.stderr


def test_profile_entry_imports_cli_after_selection(tmp_path):
    script="""
import sys, types
from monash_study_kit import entry
assert 'monash_study_kit.paths' not in sys.modules
assert 'monash_study_kit.cli' not in sys.modules
fake=types.ModuleType('monash_study_kit.cli')
def main(args):
    from monash_study_kit import paths
    assert paths.PROFILE=='alice' and paths.HOME.name=='alice'
    assert args==['help']
    print('selected before CLI')
    return 0
fake.main=main
sys.modules['monash_study_kit.cli']=fake
assert entry.main(['help','--profile=alice'])==0
"""
    result=child(tmp_path,[],script)
    assert result.returncode == 0 and "selected before CLI" in result.stdout, result.stderr


def test_profile_settings_file_cannot_disable_mode_or_escape_files(tmp_path):
    directory=tmp_path/"home/profiles/alice"
    directory.mkdir(parents=True)
    (directory/"settings.json").write_text(json.dumps({"materials_only":False,"files_dir":str(tmp_path/"outside")}), encoding="utf-8")
    result=child(tmp_path,["--profile","alice"])
    d=json.loads(result.stdout)
    assert d["materials"] is True and d["FILES_DIR"] == str(directory/"files")


def test_materials_mcp_honors_use_ed_false(monkeypatch):
    monkeypatch.setattr(M,"load_settings",lambda:{"materials_only":True,"use_ed":False})
    assert {t["name"] for t in M.tool_list()} == {"list_files","read_file","search_content"}


def test_named_profile_cli_blocks_config_even_with_false_env(tmp_path):
    result=child(tmp_path,["--profile","alice","config","materials_only","false"],
                 "from monash_study_kit.entry import main; import sys; sys.exit(main())",extra={"MONASH_KIT_MATERIALS_ONLY":"false"})
    assert result.returncode == 1 and "强制启用" in result.stderr


def test_missing_duplicate_and_literal_profile_flags(tmp_path):
    for flags in (["--profile"],["--profile=alice","--profile=bob"]):
        result=child(tmp_path,flags)
        assert result.returncode != 0
    d=json.loads(child(tmp_path,["--","--profile","literal"]).stdout)
    assert d["profile"] is None and d["rest"] == ["--","--profile","literal"]


def test_entry_refuses_switch_after_paths_are_initialized(tmp_path):
    script="""
from monash_study_kit import paths, entry
assert entry.main(['--profile','alice','help']) == 1
assert paths.PROFILE is None
"""
    result=child(tmp_path,[],script)
    assert result.returncode == 0 and "路径已初始化" in result.stderr


@pytest.mark.parametrize("url",["https://learning.monash.edu:invalid/mod/resource/view.php?id=1", "https://[invalid/mod/resource/view.php?id=1"])
def test_moodle_get_rejects_malformed_url_before_handler(url):
    args=cli.build_parser().parse_args(["moodle","get",url])
    assert "地址无效" in policy.command_error(args,{"materials_only":True})


def test_materials_mcp_rejects_all_names_outside_list_including_legacy_aliases(monkeypatch):
    monkeypatch.setattr(M,"load_settings",lambda:{"materials_only":True})
    for alias in M.LEGACY_TOOL_ALIASES:
        reply=M.handle({"id":1,"method":"tools/call","params":{"name":alias,"arguments":{}}})
        assert "error" in reply, alias
