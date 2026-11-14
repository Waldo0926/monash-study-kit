import hashlib
import io
import zipfile

import pytest

from monash_study_kit import mcp_server as M, submissions as S

URL = 'https://learning.monash.edu/pluginfile.php/123/assignsubmission_file/submission_files/456/minutes.pdf?forcedownload=1'
FEEDBACK = 'https://learning.monash.edu/pluginfile.php/123/assignfeedback_file/feedback_files/789/marked.docx'
PAGE = f'''<title>FIT1000: Week 12</title>
<div id="intro"><a href="https://learning.monash.edu/pluginfile.php/123/mod_assign/intro/spec.pdf">Spec</a></div>
<div class="submissionstatustable"><table><tr><th>Submission status</th><td>Submitted for grading</td></tr>
<tr><th>File submissions</th><td><a href="{URL}">minutes.pdf</a><a href="https://evil.test/secret">evil</a></td></tr></table>
<div class="assignsubmission_onlinetext">Our online submission</div></div>
<div class="feedback"><table><tr><th>Grade</th><td>80.80</td></tr></table><div class="gradingform_rubric">Evidence: D; Communication: HD</div>
<a href="{FEEDBACK}">Marked document</a><div>Full feedback comments</div></div>'''


class Client:
    def html(self, url):
        assert url.endswith('/mod/assign/view.php?id=12345')
        return PAGE


def test_live_details_exclude_instructions_and_external_files():
    d = S.public(S.details(Client(), 12345))
    assert [f['name'] for f in d['files']] == ['minutes.pdf', 'marked.docx']
    assert [f['kind'] for f in d['files']] == ['submission', 'feedback']
    assert 'Evidence: D' in d['feedback'] and 'Full feedback' in d['feedback']
    assert d['online_text'] == 'Our online submission'
    assert '_url' not in str(d) and 'Spec' not in str(d)


def test_legacy_tools_discover_and_read_submission(monkeypatch):
    monkeypatch.setattr(M, 'client', lambda: Client())
    monkeypatch.setattr(M, '_course', lambda ref: {'id': 1})
    monkeypatch.setattr(M.F, 'assignments', lambda *a: [{'cmid': 12345, 'missing': False}])
    listing = M.t_assignments({'course': 'FIT1000'})
    d = M.t_read_file({'path': listing[0]['read_path']})
    assert d['files'] and '80.80' in d['feedback']


def test_download_and_read_requires_live_file_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(S.moodlelib, 'DATA_DIR', tmp_path)
    downloaded = []
    def download(c, url, dest):
        downloaded.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'%PDF-test')
    monkeypatch.setattr(S, '_download', download)
    key = hashlib.sha256(URL.encode()).hexdigest()[:24]
    rel, path = S.read(Client(), f'submission:12345/{key}')
    assert path.is_file() and downloaded == [URL]
    assert S.root() in path.parents
    # Removing the original attachment must not authorize its cached copy.
    class Removed(Client):
        def html(self, url):
            return PAGE.replace(URL, 'https://evil.test/gone')
    with pytest.raises(ValueError, match='旧缓存'):
        S.read(Removed(), f'submission:12345/{key}')
    for ref in ['submission:0', 'submission:12345/../../secret', 'submission:https://evil.test']:
        with pytest.raises(ValueError):
            S.read(Client(), ref)


def test_zip_document_is_extracted_as_document(tmp_path):
    doc = io.BytesIO()
    with zipfile.ZipFile(doc, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="urn:w"><w:p><w:t>Minutes &amp; actions</w:t></w:p></w:document>')
    path = tmp_path / 'submission.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('folder/minutes.docx', doc.getvalue())
    assert 'Minutes & actions' in M._read_path({'inner': 'folder/minutes.docx'}, path, 'unused')


def test_bounded_download_and_cookie_isolation(tmp_path, monkeypatch):
    from email.message import Message
    class Response(io.BytesIO):
        status = 200
        headers = Message()
    Response.headers['Content-Type'] = 'application/pdf'
    requests = []
    class Opener:
        def open(self, req, timeout):
            requests.append(req)
            return Response(b'123456')
    monkeypatch.setattr(S.urllib.request, 'build_opener', lambda *a: Opener())
    class Cookies:
        jar = {'MoodleSession': 'private'}
        def _absorb_cookies(self, headers):
            pass
    dest = tmp_path / 'file.pdf'
    S._download(Cookies(), URL, dest)
    assert requests[-1].get_header('Cookie') == 'MoodleSession=private'
    S._download(Cookies(), 'https://abc.cloudfront.net/file', dest)
    assert requests[-1].get_header('Cookie') is None
    monkeypatch.setattr(S, 'MAX_BYTES', 2)
    with pytest.raises(ValueError, match='上限'):
        S._download(Cookies(), URL, dest)
    assert dest.read_bytes() == b'123456'  # failed refresh keeps last complete file
    assert not list(tmp_path.glob('*.part'))
    with pytest.raises(ValueError, match='不支持'):
        S._download(Cookies(), 'https://evil.test/file', dest)



def test_materials_profile_blocks_submission_before_authentication(monkeypatch):
    monkeypatch.setattr(S, 'load_settings', lambda: {'materials_only': True})
    monkeypatch.setattr(M, 'client', lambda: pytest.fail('Must not construct authenticated client'))
    with pytest.raises(ValueError, match='课程资料模式'):
        M.t_read_file({'path': 'submission:12345'})
    with pytest.raises(ValueError, match='课程资料模式'):
        M.t_assignments({'assignment_id': 12345})
    with pytest.raises(ValueError, match='课程资料模式'):
        S.details(Client(), 12345)


def test_submission_pdf_page_range_and_character_pagination(tmp_path, monkeypatch):
    path = tmp_path / 'submission.pdf'
    path.write_bytes(b'%PDF-fixture')
    monkeypatch.setattr(M, 'client', lambda: Client())
    monkeypatch.setattr(S, 'read', lambda *a: ('submission:12345/key', path))
    def pages(p, start=None, end=None):
        assert p == path and (start, end) == (2, 3)
        return ['second page', 'third page']
    monkeypatch.setattr(M.textextract, 'pdf_pages', pages)
    text = M.t_read_file({'path': 'submission:12345/key', 'page_start': 2, 'page_end': 3})
    assert 'second page' in text and 'third page' in text and '第 2 页' in text
    assert 'offset=10' in M.t_read_file({'path': 'submission:12345/key', 'page_start': 2, 'page_end': 3, 'max_chars': 10})


def test_cli_submission_detail_and_materials_policy(monkeypatch, capsys):
    from monash_study_kit import cli, materials_policy
    import json
    monkeypatch.setattr(cli, 'MoodleClient', lambda: Client())
    args = cli.build_parser().parse_args(['moodle', 'submission', '12345', '--json'])
    assert materials_policy.command_error(args, {'materials_only': True})
    args.fn(args)
    data = json.loads(capsys.readouterr().out)
    assert data['files'] and 'Full feedback' in data['feedback']


def test_zip_preserves_extensionless_text_without_decoding_binary(tmp_path):
    path = tmp_path / 'source.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('LICENSE', 'Example license text')
        z.writestr('binary.bin', b'\x00\x01\x02')
    assert M._read_path({'inner': 'LICENSE'}, path, 'source.zip') == 'Example license text'
    with pytest.raises(ValueError, match='二进制'):
        M._read_path({'inner': 'binary.bin'}, path, 'source.zip')


def test_short_download_keeps_previous_complete_cache(tmp_path, monkeypatch):
    from email.message import Message
    class Response(io.BytesIO):
        status = 200
        headers = Message()
    Response.headers['Content-Length'] = '10'
    Response.headers['Content-Type'] = 'application/pdf'
    class Opener:
        def open(self, req, timeout):
            return Response(b'short')
    monkeypatch.setattr(S.urllib.request, 'build_opener', lambda *a: Opener())
    dest = tmp_path / 'cached.pdf'
    dest.write_bytes(b'complete old file')
    class Cookies:
        jar = {}
        def _absorb_cookies(self, headers):
            pass
    with pytest.raises(ValueError, match='不完整'):
        S._download(Cookies(), URL, dest)
    assert dest.read_bytes() == b'complete old file'
    assert not list(tmp_path.glob('*.part'))


def test_collapsed_online_text_is_scoped_to_submission():
    class Inline(Client):
        def html(self, url):
            return PAGE.replace('<div id="intro">', '<div id="intro"><div class="assignsubmission_onlinetext">Assignment instruction</div>').replace(
                'class="assignsubmission_onlinetext">Our', 'class="full_assignsubmission_onlinetext_99">Our')
    assert S.details(Inline(), 12345)['online_text'] == 'Our online submission'


@pytest.mark.parametrize('url', ['https://user@abc.cloudfront.net/f', 'https://abc.cloudfront.net:444/f', 'http://abc.cloudfront.net/f'])
def test_download_rejects_unsafe_redirect_authorities(tmp_path, url):
    with pytest.raises(ValueError, match='不支持'):
        S._download(None, url, tmp_path / 'f')


def test_grade_assignment_path_validates_origin_and_activity():
    assert S.assignment_path('/mod/assign/view.php?id=12345') == 'submission:12345'
    for url in ['https://evil.test/mod/assign/view.php?id=12345', '/mod/quiz/view.php?id=12345', '/mod/assign/view.php?id=-1']:
        assert S.assignment_path(url) is None
