"""Ed Discussion 抓取库：API 客户端 + 内容解析 + SQLite 存储。"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .paths import DATA_DIR, SECRETS_DIR, write_secret

API_BASE = "https://edstem.org/api"
WEB_BASE = "https://edstem.org/au/courses"
TOKEN_PAGE = "https://edstem.org/au/settings/api-tokens"
UA = "monash-study-kit (personal study helper)"   # Ed 对 Python 默认 UA 回 403

TOKEN_FILE = SECRETS_DIR / "ed-token"
CONFIG_FILE = DATA_DIR / "ed-config.json"
DB_PATH = DATA_DIR / "ed.db"


class EdAuthError(RuntimeError):
    """token 失效/过期。"""


class EdForbidden(RuntimeError):
    """token 没问题，但这个东西看不到了（帖子被删、被改成私密等）。Ed 回 403。"""


# --------------------------------------------------------------------------
# token / config
# --------------------------------------------------------------------------

def load_token() -> str:
    tok = os.environ.get("ED_TOKEN")
    if tok:
        return tok.strip()
    if not TOKEN_FILE.exists():
        raise EdAuthError(f"还没设置 Ed 令牌，运行 `monash login ed`（令牌在 {TOKEN_PAGE} 创建）")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def save_token(token: str) -> None:
    write_secret(TOKEN_FILE, token.strip() + "\n")


def is_api_token(token: str) -> bool:
    """Ed 设置页（settings/api-tokens）生成的 API 令牌不过期，形如 xxxx.yyyy（两段）；
    从 localStorage 取的是三段式 JWT，14 天过期。两种令牌走的请求头不一样。"""
    return token.count(".") != 2


def auth_headers(token: str) -> dict:
    if is_api_token(token):
        return {"authorization": f"Bearer {token}"}
    return {"x-token": token}


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# API 客户端
# --------------------------------------------------------------------------

class EdClient:
    def __init__(self, token: str | None = None, delay: float = 0.25):
        self.token = token or load_token()
        self.delay = delay          # 全局最小请求间隔，别把 Ed 打疼
        self._last = 0.0
        self._lock = threading.Lock()   # 并行抓取时，限流必须是全局的

    def _throttle(self) -> None:
        with self._lock:
            gap = self.delay - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()

    def get(self, path: str, params: dict | None = None, retries: int = 4):
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            **auth_headers(self.token),
            "accept": "application/json",
            "user-agent": UA,
        })
        for attempt in range(retries):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise EdAuthError(
                        f"Ed 返回 401：令牌失效（{path}）。在 Ed 设置页新建一个令牌，再运行 `monash login ed`"
                    ) from e
                if e.code == 403:
                    raise EdForbidden(f"Ed 返回 403：没有权限看 {path}（被删除、改成私密，或不在这门课里）") from e
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 1.5)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * 1.5)
                    continue
                raise
        raise RuntimeError(f"请求失败：{path}")


    def me(self) -> dict:
        return self.get("/user")

    def courses(self) -> list[dict]:
        data = self.me()
        out = []
        for entry in data.get("courses", []):
            c = entry.get("course", {})
            out.append({
                "id": c.get("id"),
                "code": c.get("code"),
                "name": c.get("name"),
                "year": c.get("year"),
                "session": c.get("session"),
                "status": c.get("status"),
                "role": entry.get("role"),
            })
        return out

    def threads(self, course_id: int, limit: int = 100) -> list[dict]:
        """拉一门课的全部帖子列表（分页直到拉空）。"""
        all_threads, seen, offset = [], set(), 0
        while True:
            data = self.get(f"/courses/{course_id}/threads",
                            {"limit": limit, "offset": offset, "sort": "new"})
            batch = data.get("threads", [])
            if not batch:
                break
            fresh = [t for t in batch if t["id"] not in seen]
            for t in fresh:
                seen.add(t["id"])
            all_threads.extend(fresh)
            if len(batch) < limit:
                break
            offset += limit
            if offset > 20000:      # 保险丝
                break
        return all_threads

    def thread(self, thread_id: int) -> dict:
        """抓单个帖子的完整内容。

        千万别加 ?view=1 —— 那个参数会把帖子的 updated_at 顶成当前时间。
        我们拿 updated_at 当变更指纹，加了它就等于每抓一次都把指纹改一次，
        增量同步会永远退化成全量（而且每次都把 177 个帖子的时间戳再刷一遍）。
        不带 view=1 返回的正文、回复、users 完全一样。
        """
        return self.get(f"/threads/{thread_id}")


# --------------------------------------------------------------------------
# Ed 的 XML 文档 -> Markdown
# --------------------------------------------------------------------------
#
# 帖子/回复有两个正文字段：`document` 是 Ed 自己渲染的纯文本，会把 <link href> 的网址、
# 附件、视频全扔掉；`content` 才是原始 XML。所以一律读 content 再转 Markdown。

_TAG = re.compile(r"<[^>]+>")


def _md_inline(el) -> str:
    """段落内部：粗体、斜体、行内代码、链接、@提及。"""
    out = [el.text or ""]
    for ch in el:
        out.append(_md_node(ch, inline=True))
        out.append(ch.tail or "")
    return "".join(out)


def _wrap(mark: str, inner: str) -> str:
    # **  x ** 在 Markdown 里不算粗体，把空白挪到标记外面
    core = inner.strip()
    if not core:
        return inner
    lead = inner[:len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _md_list(el) -> str:
    """列表项里的内容（含嵌套列表）先按顶层渲染，续行统一缩进到项目符号后面。"""
    numbered = el.get("style") == "number"
    lines = []
    for i, item in enumerate([c for c in el if c.tag == "list-item"], 1):
        bullet = f"{i}." if numbered else "-"
        body = _md_blocks(item).replace("\n\n", "\n")
        first, *rest = body.split("\n")
        lines.append(f"{bullet} {first}")
        lines += [" " * (len(bullet) + 1) + r if r else r for r in rest]
    return "\n".join(lines)


def _md_node(el, inline: bool = False) -> str:
    t = el.tag
    if t == "bold":
        return _wrap("**", _md_inline(el))
    if t == "italic":
        return _wrap("*", _md_inline(el))
    if t == "strike":
        return _wrap("~~", _md_inline(el))
    if t == "underline":
        return _md_inline(el)
    if t == "code":
        return f"`{''.join(el.itertext())}`"
    if t == "link":
        text = _md_inline(el).strip()
        href = el.get("href") or ""
        if not href or text == href:
            return href or text
        return f"[{text or href}]({href})"
    if t == "mention":
        return "@" + "".join(el.itertext())
    if t == "break":
        return "\n"
    if t == "math":
        return f"${''.join(el.itertext())}$"
    if t == "image":
        return f"![图片]({el.get('src', '')})"
    if t == "file":
        return f"[📎 {el.get('filename') or '附件'}]({el.get('url', '')})"
    if t == "video":
        return f"[视频]({el.get('src', '')})"
    if inline:
        return _md_inline(el)
    # ---- 块级 ----
    if t == "paragraph":
        return _md_inline(el)
    if t == "heading":
        return "#" * int(el.get("level") or 2) + " " + _md_inline(el).strip()
    if t == "list":
        return _md_list(el)
    if t in ("pre", "snippet"):
        lang = el.get("language") or ""
        if lang in ("none", "plain"):
            lang = ""
        code = "".join(el.itertext()).rstrip("\n")
        return f"```{lang}\n{code}\n```"
    if t in ("blockquote", "callout"):
        inner = _md_blocks(el)
        return "\n".join("> " + l if l else ">" for l in inner.split("\n"))
    if t == "figure":
        return _md_blocks(el)
    return _md_blocks(el) if len(el) else "".join(el.itertext())


def _md_blocks(el) -> str:
    blocks = []
    if (el.text or "").strip():
        blocks.append(el.text.strip())
    for ch in el:
        md = _md_node(ch)
        if md.strip():
            blocks.append(md.rstrip())
        if (ch.tail or "").strip():
            blocks.append(ch.tail.strip())
    return "\n\n".join(blocks)


def _regex_to_text(s: str) -> str:
    """XML 解析失败时的兜底，至少把网址留下。"""
    s = re.sub(r"</?(pre|snippet)[^>]*>", "\n```\n", s)
    s = re.sub(r"</?(paragraph|heading|list-item|callout|blockquote)[^>]*>", "\n", s)
    s = re.sub(r"<break[^>]*/?>", "\n", s)
    s = re.sub(r'<image[^>]*src="([^"]*)"[^>]*/?>', r"\n![图片](\1)\n", s)
    s = re.sub(r'<file[^>]*url="([^"]*)"[^>]*filename="([^"]*)"[^>]*/?>', r"\n[📎 \2](\1)\n", s)
    s = re.sub(r'<video[^>]*src="([^"]*)"[^>]*/?>', r"\n[视频](\1)\n", s)
    s = re.sub(r'<link[^>]*href="([^"]*)"[^>]*>(.*?)</link>', r"[\2](\1)", s, flags=re.S)
    s = _TAG.sub("", s)
    return html.unescape(s)


def doc_to_text(document: str | None) -> str:
    """把 Ed 的正文转成 Markdown。

    传 content（<document> XML）才能保住链接和附件；传 document 纯文本就原样返回。
    """
    if not document:
        return ""
    s = document.strip()
    if not s.startswith("<document"):
        return s
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(s)
        out = _md_blocks(root)
    except Exception:
        out = _regex_to_text(s)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def body_of(obj: dict) -> str:
    """帖子或回复的正文 Markdown：优先 content（XML），没有才退回 document 纯文本。"""
    return doc_to_text(obj.get("content") or obj.get("document"))


def doc_attachments(document: str | None) -> list[dict]:
    """正文里的附件、图片、视频：[{kind, name, url}]。"""
    if not document or not document.lstrip().startswith("<document"):
        return []
    out = []
    for m in re.finditer(r"<(file|image|video)\b([^>]*)/?>", document):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        url = html.unescape(attrs.get("url") or attrs.get("src") or "")
        if not url:
            continue
        kind = m.group(1)
        name = attrs.get("filename") or url.rstrip("/").rsplit("/", 1)[-1]
        out.append({"kind": kind, "name": html.unescape(name), "url": url})
    return out


def users_index(users) -> dict:
    """Ed 的 users 有两种形态：API 直接返回的是 list，browser_export 存的是 {id: {...}}。
    统一成 {str(id): {...}}。"""
    if not users:
        return {}
    if isinstance(users, dict):
        return {str(k): v for k, v in users.items()}
    return {str(u["id"]): u for u in users if isinstance(u, dict) and u.get("id") is not None}


def _lookup(obj: dict, users: dict | None) -> dict:
    u = obj.get("user") or {}
    if not u and users:
        u = users.get(str(obj.get("user_id"))) or {}
    return u


def _is_staff(obj: dict, users: dict | None) -> bool:
    u = _lookup(obj, users)
    return (u.get("course_role") or u.get("role")) in ("admin", "staff")


ROLE_LABEL = {"admin": "教师", "staff": "助教", "student": "同学"}


def author_of(obj: dict, users=None) -> str:
    """回帖作者名。users 可以是 Ed 返回的 list，也可以是 {id: {...}} 映射。"""
    if obj.get("is_anonymous"):
        return "匿名"
    users = users_index(users) if not isinstance(users, dict) or (
        users and not isinstance(next(iter(users.values()), None), dict)) else users
    u = _lookup(obj, users)
    name = u.get("name") or "未知"
    role = u.get("course_role") or u.get("role")
    if role and role != "student":
        name += f"·{ROLE_LABEL.get(role, role)}"
    return name


def flatten_replies(thread: dict, users=None) -> list[dict]:
    """把 answers/comments 的嵌套结构拍平成有序列表。"""
    users = users_index(users)
    out: list[dict] = []

    def walk(items, kind, depth, parent_id):
        for it in items or []:
            if it.get("deleted_at"):
                continue
            out.append({
                "id": it.get("id"),
                "parent_id": parent_id,
                "kind": kind,
                "depth": depth,
                "user_id": it.get("user_id"),
                "author": author_of(it, users),
                "is_staff": _is_staff(it, users),
                "is_endorsed": bool(it.get("is_endorsed")),
                "is_anonymous": bool(it.get("is_anonymous")),
                "vote_count": it.get("vote_count") or 0,
                "created_at": it.get("created_at"),
                "updated_at": it.get("updated_at"),
                "text": body_of(it),
                "attachments": doc_attachments(it.get("content")),
            })
            walk(it.get("comments"), "reply", depth + 1, it.get("id"))
            walk(it.get("replies"), "reply", depth + 1, it.get("id"))

    walk(thread.get("answers"), "answer", 0, None)
    walk(thread.get("comments"), "comment", 0, None)
    return out


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY, code TEXT, name TEXT, year TEXT, session TEXT,
    status TEXT, tracked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY, course_id INTEGER, number INTEGER, type TEXT,
    title TEXT, category TEXT, subcategory TEXT, author TEXT, user_id INTEGER,
    is_private INTEGER, is_pinned INTEGER, is_staff_answered INTEGER,
    is_student_answered INTEGER, is_answered INTEGER, is_endorsed INTEGER,
    reply_count INTEGER, view_count INTEGER, vote_count INTEGER,
    created_at TEXT, updated_at TEXT, body TEXT, url TEXT,
    first_seen TEXT, last_fetched TEXT, content_hash TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY, thread_id INTEGER, parent_id INTEGER, kind TEXT,
    depth INTEGER, user_id INTEGER, author TEXT, is_endorsed INTEGER,
    vote_count INTEGER, created_at TEXT, updated_at TEXT, text TEXT,
    first_seen TEXT, is_staff INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    new_threads INTEGER, new_replies INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    thread_id INTEGER, reply_id INTEGER DEFAULT 0, kind TEXT, name TEXT, url TEXT,
    PRIMARY KEY (thread_id, reply_id, url)
);
-- 帖子列表里“跟我有关”的状态：收藏、关注、看过没有、有几条我没看过的回复、采纳了哪条。
-- 这些变了 updated_at 不会变，所以不进 content_hash，每次同步按列表整批覆盖。
CREATE TABLE IF NOT EXISTS thread_state (
    thread_id INTEGER PRIMARY KEY, is_starred INTEGER, is_watched INTEGER, is_seen INTEGER,
    new_reply_count INTEGER, accepted_id INTEGER, is_mine INTEGER, synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_course ON threads(course_id, created_at);
CREATE INDEX IF NOT EXISTS idx_replies_thread ON replies(thread_id, created_at);
"""


def save_attachments(conn: sqlite3.Connection, thread_id: int, thread: dict, replies: list[dict]) -> None:
    """整帖重写附件清单（帖子正文 reply_id=0，回复里的记回复 id）。"""
    conn.execute("DELETE FROM attachments WHERE thread_id=?", (thread_id,))
    rows = [(thread_id, 0, a) for a in doc_attachments(thread.get("content"))]
    rows += [(thread_id, r["id"], a) for r in replies for a in r.get("attachments") or []]
    conn.executemany(
        "INSERT OR IGNORE INTO attachments (thread_id, reply_id, kind, name, url) VALUES (?,?,?,?,?)",
        [(tid, rid, a["kind"], a["name"], a["url"]) for tid, rid, a in rows])


def save_thread_state(conn: sqlite3.Connection, listing: list[dict], me: int | None) -> None:
    """用帖子列表整批覆盖 thread_state。is_watched 为 null 表示没关注（Ed 只对关注的给 true）。"""
    ts = now_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO thread_state VALUES (?,?,?,?,?,?,?,?)",
        [(t["id"], int(bool(t.get("is_starred"))), int(bool(t.get("is_watched"))), int(bool(t.get("is_seen"))),
          t.get("new_reply_count") or 0, t.get("accepted_id"), int(me is not None and t.get("user_id") == me), ts)
         for t in listing])


def db_connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
