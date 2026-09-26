"""从 Ed 的 "Week N ... Recording" 帖子里把录播拿回来，放进 Moodle 课件的周目录。

    Zoom 录像 → 用帖子里的 Passcode 走一遍网页流程，下载 mp4（之后交给 transcribe.py 识别）
    YouTube   → 不下视频，直接取 YouTube 自动字幕，写成 .transcript.md

帖子来自本地的 ed.db。Ed 课程和 Moodle 课程按课号对应（FIT2102 S2 2026 Malaysia → FIT2102）。
链接用 Ed 令牌调 /api/threads/{id} 拿原始内容（库里的正文是转过的 Markdown，偶尔会漏）；
拿不到就退回库里的正文。

YouTube 字幕借 yt-dlp（装了 [media] 才有；它解 YouTube 要 deno，也在 [media] 里）。
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from . import edlib
from .edlib import DB_PATH as ED_DB
from .moodlelib import FILES_DIR, MoodleClient, get_state, now_iso, set_state
from .syncer import clean, course_folder, plan_layout, safe_name, week_folder

BIN_DIR = Path(sys.executable).parent      # 装在同一个环境里的 deno 在这里（Windows 是 Scripts）
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

ZOOM_RE = re.compile(r"https://[\w.-]*zoom\.us/rec/(?:share|play)/[^\s<>\"')\]]+")
YT_RE = re.compile(r"https://(?:youtu\.be/|(?:www\.)?youtube\.com/watch\?v=)([\w-]{11})")
PASS_RE = re.compile(r"Pass(?:code|word)\s*:?\s*([^\s<]+)", re.I)
WEEK_RE = re.compile(r"\bweek\s*0*(\d+)", re.I)


# ---------------------------------------------------------------- 从 Ed 找录播

def _ed_raw(thread_id: int) -> str | None:
    """Ed 原始内容（带超链接）。不能带 ?view=1——那会把帖子的 updated_at 顶成现在。"""
    try:
        return edlib.EdClient().thread(thread_id)["thread"]["content"]
    except Exception:  # noqa: BLE001 —— 令牌失效、网络问题：退回库里的正文
        return None


def extract_links(content: str) -> list[dict]:
    """从一段 Ed 内容里按出现顺序取出 Zoom / YouTube 链接，Zoom 配上它后面最近的 Passcode。"""
    text = html.unescape(content)
    found = []
    for m in re.finditer(r'href="([^"]+)"|(https://[^\s<>"]+)', text):
        url = (m.group(1) or m.group(2)).rstrip(".,;")
        pos = m.end()
        if ZOOM_RE.match(url):
            url = ZOOM_RE.match(url).group(0)
            pw = PASS_RE.search(re.sub(r"<[^>]+>", " ", text[pos:pos + 400]))
            found.append({"kind": "zoom", "url": url, "passcode": pw.group(1) if pw else None})
        elif YT_RE.match(url):
            found.append({"kind": "youtube", "url": url, "video_id": YT_RE.match(url).group(1)})
    out, seen = [], set()
    for f in found:
        key = f.get("video_id") or f["url"].split("?")[0]
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def ed_recordings() -> list[dict]:
    if not ED_DB.exists():
        return []
    con = sqlite3.connect(f"file:{ED_DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT t.id, c.code, t.number, t.title, t.body FROM threads t"
                       " JOIN courses c ON c.id = t.course_id"
                       " WHERE c.tracked = 1 AND t.title LIKE '%Recording%' ORDER BY t.created_at").fetchall()
    con.close()
    out = []
    for tid, ed_code, number, title, body in rows:
        m = re.search(r"[A-Z]{3}\d{4}", ed_code or "")
        code = m.group(0) if m else None
        week = WEEK_RE.search(title or "")
        if not code or not week:
            continue
        links = extract_links(_ed_raw(tid) or body or "")
        for i, link in enumerate(links, 1):
            out.append({**link, "code": code, "week": int(week.group(1)), "title": clean(title), "ed_number": number,
                        "part": i if len(links) > 1 else None})
    return out


# ---------------------------------------------------------------- Zoom

def zoom_download(share_url: str, passcode: str | None, dest_for, exists=lambda k: False) -> list[dict]:
    """Zoom 录像分享页：验证密码 → share-info 拿播放页 → play/info 拿 mp4 地址 → 下载。

    一场会议中途断开重连，Zoom 会把录像拆成好几段（totalClips）。第一段常常只是开场几分钟，
    正课在后面，所以要顺着 nextClipStartTime 把每一段都拿下来。dest_for(k, total) 给出第 k 段
    存到哪；exists(k) 为真的段跳过（已经下过）。

    yt-dlp 的 Zoom 解析在"验证密码之后"这一步跟不上现在的接口（2026-09），所以照网页自己走。
    """
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Referer", share_url)]
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(share_url))
    page = op.open(share_url, timeout=30).read().decode("utf-8", "replace")
    mid = re.search(r"meetingId:\s*'([^']+)'", page)
    if passcode and mid:
        body = urllib.parse.urlencode({"id": mid.group(1), "passwd": passcode, "action": "viewdetailpage",
                                       "recaptcha": ""}).encode()
        v = json.loads(op.open(base + "/nws/recording/1.0/validate-meeting-passwd", body, timeout=30).read())
        if not v.get("status"):
            raise RuntimeError(f"Zoom 密码验证失败：{v.get('errorMessage') or v.get('errorCode')}")
    if "/rec/share/" in share_url:
        share_id = share_url.split("/rec/share/", 1)[1].split("?")[0]
        si = json.loads(op.open(f"{base}/nws/recording/1.0/play/share-info/{share_id}", timeout=30).read())
        m = re.search(r"/rec/play/([^?]+)", (si.get("result") or {}).get("redirectUrl") or "")
        if not m:
            raise RuntimeError(f"Zoom 没给出播放页：{si.get('errorMessage') or si.get('errorCode')}")
        file_id = m.group(1)
    else:
        file_id = share_url.split("/rec/play/", 1)[1].split("?")[0]
    info_url = (f"{base}/nws/recording/1.0/play/info/{file_id}?canPlayFromShare=true"
                "&from=share_recording_detail&continueMode=true&componentName=rec-play")
    out, start, seen = [], None, set()
    while True:
        info = json.loads(op.open(info_url + (f"&startTime={start}" if start else ""), timeout=30).read())
        res = info.get("result") or {}
        clip, total = int(res.get("currentClip") or 1), int(res.get("totalClips") or 1)
        if clip in seen:
            break
        seen.add(clip)
        mp4 = res.get("viewMp4Url") or res.get("mp4Url")
        if not mp4:
            raise RuntimeError(f"Zoom 没给出第 {clip} 段的视频地址：{info.get('errorMessage') or info.get('errorCode')}")
        dest = dest_for(clip, total)
        if not exists(clip):
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            req = urllib.request.Request(mp4, headers={"User-Agent": UA, "Referer": base + "/"})
            with op.open(req, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            os.replace(tmp, dest)
        out.append({"clip": clip, "total": total, "path": dest, "duration": res.get("duration"),
                    "skipped": exists(clip)})
        nxt = res.get("nextClipStartTime")
        if not nxt or nxt == -1 or clip >= total:
            break
        start = nxt
    return out


# ---------------------------------------------------------------- YouTube 字幕

def youtube_transcript(video_id: str) -> tuple[str, list[tuple[float, str]]]:
    """取 YouTube 英文字幕（人工的优先，其次自动生成的）。返回 (标题, [(秒, 文字)])。"""
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "PATH": f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"}
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", "en.*,en", "--sub-format", "json3", "--print", "title", "--no-simulate",
               "-o", f"{d}/%(id)s.%(ext)s", f"https://youtu.be/{video_id}"]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
        subs = sorted(Path(d).glob("*.json3"), key=lambda p: ("orig" in p.name, "-" in p.stem.split(".")[-1]))
        if not subs:
            raise RuntimeError(f"YouTube 没有英文字幕：{(r.stderr or r.stdout)[-300:]}")
        title = (r.stdout.strip().splitlines() or [video_id])[0]
        events = json.loads(subs[0].read_text(encoding="utf-8")).get("events", [])
    lines = []
    for e in events:
        text = "".join(s.get("utf8", "") for s in e.get("segs") or []).replace("\n", " ").strip()
        if text:
            lines.append((e.get("tStartMs", 0) / 1000, text))
    return title, lines


# ---------------------------------------------------------------- 字幕稿格式（transcribe.py 也用）

def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def paragraphs(lines: list[tuple[float, str]], every: float = 45.0) -> list[tuple[float, str]]:
    """把一句句的字幕并成段落：每段大约 45 秒，碰到句号才断，读起来不碎。"""
    out, start, buf = [], None, []
    for t, text in lines:
        if start is None:
            start = t
        buf.append(text)
        if t - start >= every and re.search(r"[.?!]$", text):
            out.append((start, " ".join(buf)))
            start, buf = None, []
    if buf:
        out.append((start or 0, " ".join(buf)))
    return out


def write_transcript(path: Path, title: str, meta: list[str], lines: list[tuple[float, str]]) -> None:
    body = [f"# {title} — 字幕稿", "", " · ".join(m for m in meta if m), "",
            "> 自动生成的字幕，专有名词和代码可能听错；有疑问以视频为准。", ""]
    paras = paragraphs(lines)
    if not paras or sum(len(p.split()) for _, p in paras) < 20:
        body.append("（没有识别到讲话——可能是演示动画或没有声音的录屏。）")
    for t, text in paras:
        body += [f"**[{fmt_ts(t)}]** {text}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 入口

def week_folders(client: MoodleClient, course: dict) -> dict[int, str]:
    """周次 → 这门课在 Moodle 上的周目录名（和 sync 用的一致）。"""
    layout = plan_layout(client.course_state(course["id"]))
    out = {}
    for folder in set(layout.folders.values()):
        m = re.match(r"Week (\d+)", folder)
        if m and "/" not in folder:
            out.setdefault(int(m.group(1)), folder)
    return out


def register(con, course_id: int, source: str, rel: str, section: str, title: str) -> None:
    size = (FILES_DIR / rel).stat().st_size
    con.execute("INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at)"
                " VALUES (?,?,NULL,?,?,?,?,?) ON CONFLICT(course_id, source) DO UPDATE SET"
                " path=excluded.path, size=excluded.size, synced_at=excluded.synced_at",
                (course_id, source, rel, size, section, title, now_iso()))
    con.commit()


def sync_recordings(client: MoodleClient, con, log=print) -> dict:
    from .features import find_course
    courses = client.courses()
    stats = {"zoom": 0, "youtube": 0, "skipped": 0, "errors": []}
    folders_cache: dict[int, dict[int, str]] = {}
    for rec in ed_recordings():
        try:
            course = find_course(courses, rec["code"])
        except LookupError:
            continue
        cid = course["id"]
        if cid not in folders_cache:
            folders_cache[cid] = week_folders(client, course)
        week_dir = folders_cache[cid].get(rec["week"]) or week_folder(f"Week {rec['week']}")
        part = f" Part {rec['part']}" if rec["part"] else ""
        label = f"Week {rec['week']:02d} online seminar{part} (Ed #{rec['ed_number']})"
        rel_dir = f"{course_folder(course)}/{week_dir}"
        source = f"ed:{rec.get('video_id') or rec['url'].split('?')[0]}"
        done_key = f"ed-done:{source}"
        if get_state(con, done_key):
            stats["skipped"] += 1
            continue

        def clip_source(k):
            return source if k == 1 else f"{source}#clip{k}"

        def clip_rel(k, total):
            return f"{rel_dir}/{safe_name(label + (f' clip {k} of {total}' if total > 1 else ''))}.mp4"

        try:
            if rec["kind"] == "zoom":
                def exists(k):
                    row = con.execute("SELECT path FROM files WHERE course_id=? AND source=?",
                                      (cid, clip_source(k))).fetchone()
                    return bool(row and (FILES_DIR / row["path"]).exists())
                clips = zoom_download(rec["url"], rec.get("passcode"),
                                      lambda k, total: FILES_DIR / clip_rel(k, total), exists)
                for c in clips:
                    rel = c["path"].relative_to(FILES_DIR).as_posix()
                    if not c["skipped"]:
                        log(f"  ↓ Zoom  {rel}（{fmt_ts(c['duration'] or 0)}）")
                        stats["zoom"] += 1
                    # 第一次只下了第 1 段、文件名没带 clip 的，改成带 clip 的名字
                    old = con.execute("SELECT path FROM files WHERE course_id=? AND source=?",
                                      (cid, clip_source(c["clip"]))).fetchone()
                    if old and old["path"] != rel and (FILES_DIR / old["path"]).exists():
                        os.replace(FILES_DIR / old["path"], FILES_DIR / rel)
                    register(con, cid, clip_source(c["clip"]), rel, rec["title"],
                             rec["title"] + (f"（第 {c['clip']}/{c['total']} 段）" if c["total"] > 1 else ""))
                set_state(con, done_key, now_iso())
            else:
                title, lines = youtube_transcript(rec["video_id"])
                rel = f"{rel_dir}/{safe_name(label)}.transcript.md"
                log(f"  ✎ YouTube 字幕  {rel}")
                write_transcript(FILES_DIR / rel, f"{rec['title']}{part}",
                                 [f"来源：Ed #{rec['ed_number']}", f"YouTube《{title}》 {rec['url']}",
                                  "字幕：YouTube 自动字幕"], lines)
                register(con, cid, source, rel, rec["title"], f"字幕稿：{rec['title']}{part}")
                con.execute("INSERT OR IGNORE INTO links (course_id, folder, section, title, url, synced_at)"
                            " VALUES (?,?,?,?,?,?)", (cid, week_dir, "Recordings", f"{rec['title']}{part}",
                                                      rec["url"], now_iso()))
                con.commit()
                stats["youtube"] += 1
                set_state(con, done_key, now_iso())
        except Exception as e:  # noqa: BLE001
            if e.__class__.__name__ == "MoodleAuthError":
                raise
            stats["errors"].append(f"{label}: {e}")
            log(f"  ! {label}: {e}")
    return stats
