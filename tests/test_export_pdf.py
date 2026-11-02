"""课程 PDF 导出：只用合成资料，假 CDP 覆盖协议，真实浏览器只访问临时文件和本机测试服务。"""
import base64
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from monash_study_kit import cli, edlib, export as X, mdhtml, moodlelib, paths, pdfrender

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1sAAAAASUVORK5CYII=")


def test_markdown_blocks_and_inline():
    text = """# 标题 <test>

Paragraph **bold** *italic* `x < y` [site](https://example.invalid/?x=1&y=2)
next line

3. parent
   - child
     1. nested
4. sibling

> quoted
> **text**

---

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| a | b | c | d | e | f |

```python
if x:
    print('<hello>')
```
"""
    html = mdhtml.document(text)
    for expected in ('<title>标题 &lt;test&gt;</title>', '<h1>', '<strong>bold</strong>', '<em>italic</em>',
                     '<code>x &lt; y</code>', 'x=1&amp;y=2', '<br>', '<ol start="3">', '<ul>',
                     '<blockquote>', '<hr>', '<thead>', '<td>f</td>', "    print(&#x27;&lt;hello&gt;&#x27;)"):
        assert expected in html
    assert html.index('parent') < html.index('<ul>') < html.index('child') < html.index('nested') < html.index('sibling')


@pytest.mark.parametrize("payload", ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
                                     '<iframe src="https://example.invalid/"></iframe>', '<style>@import "x";</style>'])
def test_raw_html_is_always_escaped(payload):
    html = mdhtml.document(payload)
    assert payload not in html
    assert '&lt;' in html


@pytest.mark.parametrize("url", ["javascript:alert", "data:text/html,evil", "file:///private/key", "//evil.invalid/x", "java\tscript:evil"])
def test_unsafe_links_are_plain_text(url):
    html = mdhtml.document(f"[label]({url})")
    assert '<a href=' not in html


def test_images_only_come_from_resolver_and_links_are_rewritten():
    got = []
    html = mdhtml.document('![local](../../assets/a.png) ![remote](https://example.invalid/x)\n[page](other.md#heading)',
                           image=lambda u: got.append(u) or ('data:image/png;base64,AA==' if u.startswith('../') else None), pdf_links=True)
    assert got == ['../../assets/a.png', 'https://example.invalid/x']
    assert '<img src="data:image/png;base64,AA==" alt="local">' in html
    assert 'href="other.pdf#heading"' in html
    assert '<img src="https:' not in html and 'remote' in html
    assert '<title>page</title>' in html


def test_table_escaped_pipe_and_code_title():
    html = mdhtml.document('```\n# Not a title\n```\n\n# Real title\n\n| A | B |\n|---|---|\n| a\\|b | c |')
    assert '<title>Real title</title>' in html and '<td>a|b</td>' in html


@pytest.fixture
def course(tmp_path, monkeypatch):
    source = tmp_path / 'body.md'
    source.write_text('# Sample 正文\n\n![local](assets/pic.png)\n![remote](https://example.invalid/no.png)\n[More](Links.md)', encoding='utf-8')
    asset = tmp_path / 'pic.png'
    asset.write_bytes(PNG)
    handout = tmp_path / 'lecture.pdf'
    handout.write_bytes(b'%PDF-original')
    def moodle(p, con):
        p.add('FIT1043/Moodle/body.md', source)
        p.add('FIT1043/Moodle/assets/pic.png', asset)
        p.add('FIT1043/Moodle/Links.md', '# Links\n\n[example](https://example.invalid/)')
        p.add('FIT1043/Moodle/lecture.pdf', handout)
        p.missing.append({'where': 'Week 2', 'title': 'Missing', 'url': '', 'reason': 'synthetic missing'})
    monkeypatch.setattr(X, 'plan_moodle', moodle)
    monkeypatch.setattr(pdfrender, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(X, 'load_settings', lambda: {'export_format': 'pdf'})
    monkeypatch.setattr(pdfrender, 'find_browser', lambda: 'fake-browser')
    calls = []
    class FakeRenderer:
        def __init__(self, exe):
            calls.append(('init', exe))
        def __enter__(self):
            return self
        def render(self, html):
            calls.append(('render', html))
            return b'%PDF-fake\n' + html.encode()
        def __exit__(self, *args):
            calls.append(('close', None))
    monkeypatch.setattr(pdfrender, 'Renderer', FakeRenderer)
    return tmp_path, source, asset, calls


def make_plan(**kwargs):
    return X.plan('FIT1043', moodle_con=object(), ed_conn=None, **kwargs)


@pytest.mark.parametrize('fmt,md_count,pdf_count', [(None, 0, 4), ('both', 3, 4), ('md', 3, 1)])
def test_formats_zip_and_folder(course, fmt, md_count, pdf_count):
    import zipfile
    tmp, source, _, calls = course
    original = source.read_bytes()
    plan = make_plan(export_format=fmt)
    out = io.BytesIO()
    stats = X.write_zip(plan, out)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert sum(n.endswith('.md') for n in names) == md_count
        assert sum(n.endswith('.pdf') for n in names) == pdf_count
        assert z.read('FIT1043/Moodle/lecture.pdf') == b'%PDF-original'
        assert len(names) == len(set(names))
        assert stats['bytes'] == sum(z.getinfo(n).file_size for n in names)
        for name in names:
            if name.endswith('.md') and fmt == 'both':
                assert name[:-3] + '.pdf' in names
    X.write_dir(plan, tmp / 'out')
    assert sorted(str(p.relative_to(tmp / 'out')) for p in (tmp / 'out').rglob('*') if p.is_file()) == sorted(names)
    assert source.read_bytes() == original
    assert sum(method == 'init' for method, _ in calls) == (fmt != 'md')
    assert stats['statistics']['remote_images'] == 1
    assert stats['statistics']['link_files'] == 1
    if fmt != 'md':
        html = next(html for method, html in calls if method == 'render' and '<title>Sample' in html)
        assert 'data:image/png;base64,' in html and '![local]' not in html
        assert '<img src="https:' not in html and 'href="Links.pdf"' in html
        assert stats['pdf'] == {'generated': 3, 'cached': 0, 'skipped_reason': None}
        assert calls[-1][0] == 'close'


def test_cache_hit_and_image_invalidation(course):
    _, _, asset, calls = course
    make_plan()
    calls.clear()
    assert make_plan().pdf == {'generated': 0, 'cached': 3, 'skipped_reason': None}
    assert calls == []
    asset.write_bytes(PNG + b'changed')
    p = make_plan()
    assert p.pdf['generated'] == 1 and p.pdf['cached'] == 2


@pytest.mark.parametrize('fmt', ['pdf', 'both', 'md'])
def test_no_browser_falls_back_to_markdown(course, monkeypatch, fmt):
    monkeypatch.setattr(pdfrender, 'find_browser', lambda: None)
    p = make_plan(export_format=fmt)
    assert sum(arc.endswith('.md') for arc, _ in p.entries) == 3
    assert sum(arc.endswith('.pdf') for arc, _ in p.entries) == 1
    if fmt != 'md':
        assert pdfrender.NO_BROWSER in dict(p.entries)['FIT1043/README.md']
        assert p.pdf['skipped_reason'] == pdfrender.NO_BROWSER


def test_cli_json_format_and_fallback_message(course, monkeypatch, capsys):
    tmp, _, _, _ = course
    monkeypatch.setattr(moodlelib, 'db_connect', lambda: type('Con', (), {'close': lambda self: None})())
    monkeypatch.setattr(edlib, 'db_connect', lambda: None)
    # CLI 需要两个连接可关闭；计划里不读取 Ed。
    monkeypatch.setattr(X, 'plan_ed', lambda *args: None)
    monkeypatch.setattr(edlib, 'db_connect', moodlelib.db_connect)
    argv = ['export', 'FIT1043', '--to', str(tmp / 'cli'), '--json']
    assert cli.main(argv + ['--format', 'both']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['pdf'] == {'generated': 3, 'cached': 0, 'skipped_reason': None}
    monkeypatch.setattr(pdfrender, 'find_browser', lambda: None)
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['pdf']['skipped_reason'] == pdfrender.NO_BROWSER
    assert pdfrender.NO_BROWSER in captured.err


def test_format_validation(course):
    assert paths.DEFAULTS['export_format'] == 'pdf'
    for fmt in ('pdf', 'both', 'md'):
        assert cli.parse_setting('export_format', fmt, 'pdf') == fmt
    with pytest.raises(ValueError, match='export_format'):
        cli.parse_setting('export_format', 'html', 'pdf')
    with pytest.raises(ValueError, match='export_format'):
        make_plan(export_format='html')
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['export', 'FIT1043', '--format', 'html'])


def test_image_paths_cannot_read_unlisted_files(course):
    tmp, source, _, calls = course
    secret = tmp / 'secret.png'
    secret.write_bytes(b'PRIVATE-IMAGE')
    source.write_text(f'![secret]({secret.as_uri()})\n![escape](../secret.png)\n![data](data:image/png;base64,UFJJVkFURQ==)', encoding='utf-8')
    make_plan()
    html = next(html for method, html in calls if method == 'render' and '<title>body' in html)
    assert 'data:image/' not in html
    assert 'PRIVATE-IMAGE' not in html and 'UFJJVkFURQ==' not in html


def test_collision_preserves_original_pdf(course):
    _, source, _, _ = course
    source.with_suffix('.pdf').write_bytes(b'%PDF-original')
    # 模拟源目录中有同名正文和原件。
    original = X.plan_moodle
    def moodle(p, con):
        original(p, con)
        p.add('FIT1043/Moodle/body.pdf', source.with_suffix('.pdf'))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(X, 'plan_moodle', moodle)
        with pytest.raises(ValueError, match='路径重名'):
            make_plan()


def test_cache_prunes_oldest_and_replaces_corrupt_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfrender, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pdfrender, 'CACHE_LIMIT', 20)
    pdfrender.store('old', b'%PDF-old')
    old = next(pdfrender.cache_dir().glob('*.pdf'))
    os.utime(old, (1, 1))
    pdfrender.store('new', b'%PDF-new-data')
    assert not old.exists() and pdfrender.cached('new') == b'%PDF-new-data'
    path = next(pdfrender.cache_dir().glob('*.pdf'))
    path.write_bytes(b'broken')
    assert pdfrender.cached('new') is None
    pdfrender.store('new', b'%PDF-fixed')
    assert pdfrender.cached('new') == b'%PDF-fixed'
    assert not list(pdfrender.cache_dir().glob('*.tmp'))


@pytest.fixture
def fake_cdp(monkeypatch):
    calls = []
    class Proc:
        killed = False
        def wait(self, timeout):
            calls.append(('wait', timeout))
        def kill(self):
            self.killed = True
    class FakeCDP:
        def __init__(self, argv):
            self.proc = Proc()
            self.argv = argv
            calls.append(('argv', argv))
        def call(self, method, params=None, **kwargs):
            calls.append((method, params, kwargs))
            return {'Target.createTarget': {'targetId': 'target'},
                    'Target.attachToTarget': {'sessionId': 'session'},
                    'Runtime.evaluate': {'result': {'value': 'complete'}},
                    'Page.printToPDF': {'data': base64.b64encode(b'%PDF-test').decode()}}.get(method, {})
        def close(self):
            calls.append(('closed',))
    monkeypatch.setattr(pdfrender, 'PipeCDP', FakeCDP)
    return calls


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 管道；Windows 使用随机本机端口')
def test_cdp_a4_isolation_blocking_and_cleanup(fake_cdp):
    with pdfrender.Renderer('fake') as r:
        root = r.root
        assert root.is_dir()
        assert r.render('<html>test</html>').startswith(b'%PDF')
        r.render('<html>second</html>')
    assert not root.exists()
    argv = fake_cdp[0][1]
    assert '--remote-debugging-pipe' in argv
    assert '--disable-background-networking' in argv
    assert f'--user-data-dir={root / "profile"}' in argv
    assert str(paths.BROWSER_PROFILE) not in ' '.join(argv)
    blocked = next(c[1] for c in fake_cdp if c[0] == 'Network.setBlockedURLs')
    assert {'http://*', 'https://*'} <= set(blocked['urls'])
    assert next(c[1] for c in fake_cdp if c[0] == 'Network.emulateNetworkConditions')['offline']
    method, args, kwargs = next(c for c in fake_cdp if c[0] == 'Page.printToPDF')
    assert args['paperWidth'] == pytest.approx(210 / 25.4)
    assert args['paperHeight'] == pytest.approx(297 / 25.4)
    assert args['printBackground'] is True and kwargs['session_id'] == 'session'
    assert sum(c[0] == 'argv' for c in fake_cdp) == 1
    assert any(c[0] == 'Browser.close' for c in fake_cdp)
    assert ('closed',) in fake_cdp and ('wait', 5) in fake_cdp


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 管道')
def test_renderer_cleanup_on_print_and_startup_failure(fake_cdp, monkeypatch):
    original = pdfrender.PipeCDP.call
    def failing(self, method, *args, **kwargs):
        if method == 'Page.printToPDF':
            raise RuntimeError('synthetic failure')
        return original(self, method, *args, **kwargs)
    monkeypatch.setattr(pdfrender.PipeCDP, 'call', failing)
    with pytest.raises(RuntimeError, match='synthetic'):
        with pdfrender.Renderer('fake') as r:
            root = r.root
            r.render('test')
    assert not root.exists() and ('closed',) in fake_cdp
    def startup_fail(*args, **kwargs):
        raise OSError('synthetic startup failure')
    monkeypatch.setattr(pdfrender, 'PipeCDP', startup_fail)
    r = pdfrender.Renderer('fake')
    with pytest.raises(OSError):
        r.__enter__()
    assert not r.root.exists()


def test_real_browser_pdf_local_image_and_no_remote_requests():
    exe = pdfrender.find_browser()
    if not exe:
        pytest.skip('没有本机 Chrome / Edge / Brave')
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(PNG)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    remote = f'http://127.0.0.1:{server.server_port}/blocked.png'
    data_uri = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
    try:
        with pdfrender.Renderer(exe) as renderer:
            root = renderer.root
            table = "\n\n| A | B | C | D | E | F |\n|---|---|---|---|---|---|\n"
            table += "\n".join(f"| Row {i:03d} | 中文说明 | b | c | d | e |" for i in range(100))
            html = mdhtml.document(f'# Test 中文\n\n![local](pic.png)\n![remote]({remote})' + table,
                                   image=lambda u: data_uri if u == 'pic.png' else None)
            pdf = renderer.render(html)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf))
            assert pdf.startswith(b'%PDF') and len(reader.pages) >= 2
            assert "Row 099" in "\n".join(page.extract_text() for page in reader.pages)
            assert 'Test' in reader.pages[0].extract_text()
            assert len(reader.pages[0].images) >= 1
            # 绕过 Markdown 和 CSP 再试一次：CDP 网络阻断本身也必须生效。
            renderer.render(f'<html><body><h1>Second page</h1><img src="{remote}"></body></html>')
            result = renderer.call('Runtime.evaluate', {'expression': 'document.images[0].naturalWidth', 'returnByValue': True})
            assert result['result']['value'] == 0
        assert not root.exists()
        assert hits == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_parentheses_in_image_paths_and_link_addresses():
    got = []
    html = mdhtml.document('![figure](assets/plot(1).png) [paper](https://example.invalid/a_(b))',
                           image=lambda u: got.append(u) or None)
    assert got == ['assets/plot(1).png']
    assert 'href="https://example.invalid/a_(b)"' in html


def test_persistent_config_and_materials_only_export(course, monkeypatch, capsys):
    import argparse
    tmp, _, _, _ = course
    monkeypatch.setattr(paths, 'SETTINGS_FILE', tmp / 'settings.json')
    monkeypatch.setattr(paths, 'HOME', tmp)
    cli.cmd_config(argparse.Namespace(key='export_format', value='both'))
    assert paths.load_settings()['export_format'] == 'both'
    paths.save_settings({'materials_only': True})
    monkeypatch.setattr(X, 'load_settings', paths.load_settings)
    assert make_plan().pdf['generated'] == 3
    assert sum(arc.endswith('.md') for arc, _ in make_plan().entries) == 3
    assert 'export' in cli.build_parser().format_help()
    capsys.readouterr()


def test_windows_renderer_uses_temporary_profile_and_loopback_port(fake_cdp, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    monkeypatch.setattr(pdfrender, 'os', SimpleNamespace(name='nt'))
    cls = pdfrender.PipeCDP
    def launch(argv, **kwargs):
        profile = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--user-data-dir=')))
        profile.mkdir()
        (profile / 'DevToolsActivePort').write_text('12345\n/devtools/browser/fake\n', encoding='utf-8')
        return cls(argv).proc
    monkeypatch.setattr(pdfrender.subprocess, 'Popen', launch)
    monkeypatch.setattr(pdfrender, 'CDP', lambda url: cls([url]))
    with pdfrender.Renderer('fake') as renderer:
        root = renderer.root
        assert renderer.render('<html>Windows</html>').startswith(b'%PDF')
    assert not root.exists()
    argv = fake_cdp[0][1]
    assert '--remote-debugging-port=0' in argv and '--remote-debugging-address=127.0.0.1' in argv
    assert str(paths.BROWSER_PROFILE) not in ' '.join(argv)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 管道')
def test_renderer_kills_and_reaps_hung_process(fake_cdp, monkeypatch):
    import subprocess
    renderer = pdfrender.Renderer('fake').__enter__()
    calls = []
    def wait(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired('fake', timeout)
    monkeypatch.setattr(renderer.proc, 'wait', wait)
    renderer.close()
    assert renderer.proc.killed and len(calls) == 2
    assert not renderer.root.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 管道')
def test_pipe_cdp_start_failure_closes_all_descriptors(monkeypatch):
    from monash_study_kit import cdp
    fds = []
    pipe = os.pipe
    def tracked_pipe():
        pair = pipe()
        fds.extend(pair)
        return pair
    def failed(*args, **kwargs):
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(cdp.os, 'pipe', tracked_pipe)
    monkeypatch.setattr(cdp.subprocess, 'Popen', failed)
    with pytest.raises(OSError):
        cdp.PipeCDP(['missing-browser'])
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('configured', [None, 'missing-browser-program', 'existing'])
def test_browser_detection_rejects_stale_settings(configured, monkeypatch):
    import sys
    executable = sys.executable if configured == 'existing' else configured
    monkeypatch.setattr(pdfrender, '_find_browser', lambda: executable)
    found = pdfrender.find_browser()
    assert bool(found) == (configured == 'existing')
