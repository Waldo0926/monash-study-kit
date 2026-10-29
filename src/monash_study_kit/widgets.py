"""Optional MCP Apps views; existing tool text and arguments remain unchanged."""
from __future__ import annotations

from importlib.resources import files

URI = "ui://monash/study-panel.html"
MIME = "text/html;profile=mcp-app"
VIEW_KEY = "monash/view"
TOOLS = {"get_study_todo", "get_ed_lessons"}


def resource() -> dict:
    return {"uri": URI, "name": "Study panels", "mimeType": MIME,
            "description": "Read-only study todo, lesson progress and quiz practice.",
            "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}}}


def read_resource(uri: str) -> dict:
    if uri != URI:
        raise LookupError(f"没有资源 {uri}")
    return {"contents": [{"uri": URI, "mimeType": MIME, "_meta": resource()["_meta"],
                          "text": files("monash_study_kit").joinpath("ui/panel.html").read_text(encoding="utf-8")}]}


def progress(rows: list[dict], course: str) -> dict:
    modules: dict[str, dict] = {}
    for row in rows:
        module = modules.setdefault(row.get("module") or "其他", {
            "name": row.get("module") or "其他", "total": 0, "completed": 0, "unfinished": []})
        module["total"] += 1
        if row["status"] == "completed":
            module["completed"] += 1
        else:
            module["unfinished"].append({k: row.get(k) for k in ("id", "title", "status", "url")})
    return {"kind": "progress", "course": course, "modules": list(modules.values())}


def quiz(rows: list[dict], course: str) -> dict:
    questions = []
    for lesson in rows:
        for section in lesson["quizzes"]:
            for question in section["questions"]:
                questions.append({**question, "n": len(questions) + 1,
                                  "section": f"{lesson['lesson']} · {section['title']}"})
    return {"kind": "quiz", "course": course,
            "lesson": "、".join(row["lesson"] for row in rows), "questions": questions,
            "url": rows[0]["url"] if len(rows) == 1 else None, "quiz_title": "Lesson 练习"}


def payload(name: str, args: dict, data, ed_connection) -> dict | None:
    if name == "get_study_todo" and isinstance(data, dict):
        return {**data, "kind": "todo", "days": int(args.get("days", 7))}
    if name == "get_ed_lessons":
        if args.get("quiz"):
            from . import edquery
            rows = edquery.quizzes(ed_connection(), args["course"], args.get("module"), args.get("status"))
            if args.get("lesson"):
                ref = str(args["lesson"]).strip().casefold()
                rows = [row for row in rows if ref in row["lesson"].casefold()
                        or row["url"].rstrip("/").endswith("/" + ref)]
            return quiz(rows, args["course"])
        if not args.get("lesson") and isinstance(data, list):
            from . import edquery
            quiz_urls = {row["url"] for row in edquery.quizzes(ed_connection(), args["course"])}
            view = progress(data, args["course"])
            for module in view["modules"]:
                for lesson in module["unfinished"]:
                    lesson["has_quiz"] = lesson["url"] in quiz_urls
            return view
    return None
