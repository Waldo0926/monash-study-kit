"""录播：从 Ed 内容里认链接和密码、字幕稿的格式、转写的待办判断。"""

from monash_study_kit import recordings as R
from monash_study_kit import transcribe as T

ED_WEEK8 = ("<document><paragraph>Dear MUM Students,</paragraph><paragraph>Week 8 Online Seminar Recording,</paragraph>"
            "<paragraph>https://monash.zoom.us/rec/share/ExAmPlEsHaReId01.fakeRecordingToken_x</paragraph>"
            "<paragraph>Passcode: Ab1+cD2e</paragraph><paragraph>Link for Q&amp;A</paragraph>"
            "<paragraph>https://docs.google.com/spreadsheets/d/1hlLnsjE/edit?usp=sharing</paragraph></document>")
ED_WEEK6 = ('<document><paragraph>Week 6 is divided into 2 parts.</paragraph>'
            '<paragraph>Part 1 is <link href="https://youtu.be/AAAAAAAAAAA">here</link>.</paragraph>'
            '<paragraph>Part 2 is <link href="https://youtu.be/BBBBBBBBBBB">here</link>.</paragraph></document>')
ED_TWO_ZOOM = ("Part A https://monash.zoom.us/rec/share/AAA.111 Passcode: pa$$1 "
               "Part B https://monash.zoom.us/rec/share/BBB.222 Passcode: pa$$2")


def test_zoom_link_with_passcode():
    [z] = R.extract_links(ED_WEEK8)
    assert z == {"kind": "zoom", "url": "https://monash.zoom.us/rec/share/ExAmPlEsHaReId01.fakeRecordingToken_x",
                 "passcode": "Ab1+cD2e"}


def test_youtube_hyperlinks_are_found_in_raw_content():
    links = R.extract_links(ED_WEEK6)
    assert [l["video_id"] for l in links] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]


def test_each_zoom_link_gets_its_own_passcode():
    a, b = R.extract_links(ED_TWO_ZOOM)
    assert (a["passcode"], b["passcode"]) == ("pa$$1", "pa$$2")


def test_paragraphs_break_on_sentence_after_45s():
    lines = [(0, "Hello everyone."), (20, "Today we cover functors"), (50, "and applicatives."), (60, "Next part.")]
    paras = R.paragraphs(lines)
    assert paras == [(0, "Hello everyone. Today we cover functors and applicatives."), (60, "Next part.")]
    assert R.fmt_ts(3725) == "1:02:05" and R.fmt_ts(65) == "01:05"


def test_silent_video_says_so(tmp_path):
    p = tmp_path / "x.transcript.md"
    R.write_transcript(p, "Screensaver demo", ["视频：screensaver-example.mp4"], [(0, "music")])
    assert "没有识别到讲话" in p.read_text(encoding="utf-8")
    R.write_transcript(p, "Week 8", [], [(0, "word " * 30 + ".")])
    text = p.read_text(encoding="utf-8")
    assert "**[00:00]**" in text and "没有识别到讲话" not in text


def test_transcript_path_and_prompt():
    assert T.transcript_path("U/Week 05 - X/Week6.mp4") == "U/Week 05 - X/Week6.transcript.md"
    item = {"path": "FIT2102 Programming paradigms (S2 2026)/Week 08 - Functors and Applicatives in Haskell/zoom_1 (1).mp4",
            "fullname": "FIT2102 Programming paradigms - S2 2026"}
    assert T.prompt_for(item) == "FIT2102 Programming paradigms - S2 2026. Week 08 - Functors and Applicatives in Haskell."


def test_pending_skips_already_transcribed(tmp_path, monkeypatch):
    from monash_study_kit import moodlelib
    monkeypatch.setattr(T, "FILES_DIR", tmp_path)
    con = moodlelib.db_connect(tmp_path / "m.db")
    for rel in ("U/W1/a.mp4", "U/W1/a.transcript.md", "U/W2/b.mp4", "U/W2/notes.pdf"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
        con.execute("INSERT INTO files (course_id, source, path, synced_at) VALUES (1, ?, ?, 'now')", (rel, rel))
    assert [p["path"] for p in T.pending(con)] == ["U/W2/b.mp4"]
