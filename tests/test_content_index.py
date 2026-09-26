import sqlite3

from monash_study_kit import content_index as CI

TRANSCRIPT = """# Week 5 — 字幕稿

> 自动生成

**[01:06]** Okay, hello everyone. Today we look at git rebase.

**[1:29:32]** I did a git rebase main while I was in the second branch.
"""


def test_fts_query_quotes_every_term():
    assert CI.fts_query('docker volumes') == '"docker" AND "volumes"'
    assert CI.fts_query('"git rebase" main*') == '"git rebase" AND "main"*'
    assert CI.fts_query('a-b (c) OR') == '"a" AND "b" AND "c" AND "OR"'
    assert CI.fts_query("  ") == ""


def test_transcript_chunks_keep_timestamps():
    assert [loc for loc, _ in CI.transcript_chunks(TRANSCRIPT)] == ["01:06", "1:29:32"]


def test_index_and_search(tmp_path, monkeypatch):
    files = tmp_path / "files"
    week = files / "FIT2109 Computer science workshop (S2 2026)" / "Week 05 - Git"
    week.mkdir(parents=True)
    (week / "Week 05 online seminar.transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    (week / "Week 05 online seminar.mp4").write_bytes(b"")
    (week / "notes.md").write_text("Monads are burritos.\n\nNothing about git here.", encoding="utf-8")
    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    monkeypatch.setattr(CI, "ED_DB", tmp_path / "no-ed" / "ed.db")
    con = sqlite3.connect(":memory:")

    assert CI.index(con)["indexed"] == 2
    hits = CI.search(con, "rebase")                   # porter：rebase 也能匹配 rebase main
    assert [(h["course"], h["loc"], h["video"]) for h in hits][0][:2] in [("FIT2109", "01:06"), ("FIT2109", "1:29:32")]
    assert hits[0]["video"] == "Week 05 online seminar.mp4"
    assert hits[0]["path"].startswith("FIT2109 Computer science workshop (S2 2026)/")
    assert [h["loc"] for h in CI.search(con, '"second branch"')] == ["1:29:32"]
    assert CI.search(con, "monad")[0]["title"].endswith("notes.md")   # 词干：monad ↔ Monads
    assert CI.search(con, "monad", course="FIT2102") == []

    assert CI.index(con) == {"indexed": 0, "unchanged": 2, "removed": 0, "chunks": 0}
    (week / "notes.md").unlink()
    assert CI.index(con)["removed"] == 1
    assert CI.search(con, "burritos") == []
