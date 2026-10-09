"""把同步下来的录像（Moodle 上的 mp4、Ed 里的 Zoom 录像）转成带时间戳的字幕稿。

    monash media [--limit N] [--model small.en] [--dry-run]

每个视频旁边生成 <原名>.transcript.md，并登记进 files 表，list_files、read_file、
全文搜索都能看到，Claude 读字幕稿就知道老师讲了什么。

用 faster-whisper（CTranslate2，CPU int8），要装 [media]。普通笔记本上 small.en 大约几倍实时，
一节 2 小时的课要跑二三十分钟，所以只在手动运行 monash media 时做，不放进后台同步。
  * 内容完全一样的视频只识别一次（Moodle 上同一段录像常出现在两周），另一份写一句指过去
  * 给模型一段提示：课程名 + 这一周的标题，专有名词（Functor、beta reduction…）会准很多
  * 没有讲话的录屏（演示动画）会写明"没有识别到讲话"，不会硬编内容
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .moodlelib import FILES_DIR, now_iso
from .recordings import fmt_ts, register, write_transcript

MEDIA = (".mp4", ".mov", ".m4a", ".mp3", ".webm", ".mkv", ".wav")
MODEL = os.environ.get("MONASH_KIT_WHISPER_MODEL", "small.en")
THREADS = int(os.environ.get("MONASH_KIT_WHISPER_THREADS", str(max(1, (os.cpu_count() or 4) // 2))))


def transcript_path(media_rel: str) -> str:
    return re.sub(r"\.[^./]+$", "", media_rel) + ".transcript.md"


def file_hash(p: Path) -> str:
    """大小 + 开头 16 MB 的 SHA-1：够分辨"是不是同一个文件"，不用读完几百 MB。"""
    h = hashlib.sha1(str(p.stat().st_size).encode())
    with open(p, "rb") as f:
        h.update(f.read(16 << 20))
    return h.hexdigest()


def pending(con) -> list[dict]:
    rows = con.execute("SELECT f.course_id, f.path, f.title, f.section, c.fullname FROM files f "
                       "LEFT JOIN courses c ON c.id=f.course_id WHERE f.removed_at IS NULL ORDER BY f.path").fetchall()
    have = {r["path"] for r in rows}
    out = []
    for r in rows:
        if r["path"].lower().endswith(MEDIA) and transcript_path(r["path"]) not in have \
                and (FILES_DIR / r["path"]).exists():
            out.append(dict(r))
    return out


def prompt_for(item: dict) -> str:
    """课程名 + 周目录名，比如 "FIT2102 Programming paradigms. Week 08 - Functors and Applicatives in Haskell."。"""
    parts = item["path"].split("/")
    week = next((p for p in parts if p.startswith("Week ")), "")
    return f"{item.get('fullname') or parts[0]}. {week}.".replace("..", ".")


def run(con, *, model_name: str = MODEL, limit: int = 0, match: str | None = None, dry_run: bool = False,
        log=print) -> int:
    """转写还没有字幕稿的视频，返回处理了几个。"""
    todo = pending(con)
    if match:
        todo = [t for t in todo if match.lower() in t["path"].lower()]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} 个视频待转写（模型 {model_name}）")
    if dry_run or not todo:
        for t in todo:
            log(f"  · {t['path']}")
        return 0

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("没装转写组件。重新安装时带上 [media]：见 README 的“录播字幕”一节") from None
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=THREADS)
    # 以前几次已经转写过的视频也算进去，跨次运行一样能认出重复
    seen: dict[str, str] = {}
    have = {r["path"] for r in con.execute("SELECT path FROM files")}
    for r in con.execute("SELECT path FROM files").fetchall():
        p = r["path"]
        if p.lower().endswith(MEDIA) and transcript_path(p) in have and (FILES_DIR / p).exists():
            seen.setdefault(file_hash(FILES_DIR / p), transcript_path(p))
    for item in todo:
        media = FILES_DIR / item["path"]
        out_rel = transcript_path(item["path"])
        h = file_hash(media)
        name = Path(item["path"]).name
        title = item["title"] or Path(name).stem
        if h in seen:
            (FILES_DIR / out_rel).write_text(
                f"# {title} 字幕稿\n\n这段视频和 `{seen[h]}` 完全相同，字幕稿见那一份。\n", encoding="utf-8")
            register(con, item["course_id"], f"transcript:{item['path']}", out_rel, item["section"], f"字幕稿：{title}")
            log(f"  = {name}（和 {seen[h]} 相同）")
            continue
        t0 = time.time()
        log(f"  ♪ {item['path']}")
        segments, info = model.transcribe(str(media), language="en", beam_size=5, vad_filter=True,
                                          initial_prompt=prompt_for(item), condition_on_previous_text=False)
        lines = [(s.start, s.text.strip()) for s in segments if s.text.strip()]
        write_transcript(FILES_DIR / out_rel, title,
                         [f"视频：{name}", f"时长 {fmt_ts(info.duration)}",
                          f"识别：faster-whisper {model_name}", f"生成于 {now_iso()[:16].replace('T', ' ')} UTC"],
                         lines)
        register(con, item["course_id"], f"transcript:{item['path']}", out_rel, item["section"], f"字幕稿：{title}")
        seen[h] = out_rel
        took = time.time() - t0
        log(f"    → {out_rel}（{fmt_ts(info.duration)} 的视频用了 {fmt_ts(took)}，"
            f"{info.duration / max(took, 1):.1f}× 实时）")
    return len(todo)
