"""Local MCP Gmail cache tools. Remote exposure needs separate per-client grants.

These only read the private mirror filled by `monash gmail sync`, so they work offline and
never touch the mailbox. Mailbox writes live in student_tools and are opt-in.
"""
from . import gmail


def query_store(operation, arguments):
    store = gmail.Store()
    try:
        if operation == "status":
            return store.status()
        if operation == "todo":
            return store.inbox_todo(days=arguments.get("days", 7), limit=arguments.get("limit", 10))
        if operation == "search":
            return store.search(arguments.get("query", ""), limit=arguments.get("limit", 20), offset=arguments.get("offset", 0))
        if operation == "thread":
            return store.read_thread(arguments["thread_id"], offset=arguments.get("offset", 0), max_chars=arguments.get("max_chars", 20000))
        raise ValueError("Unknown Gmail cache operation")
    finally:
        store.close()


TOOLS = {
    "get_gmail_cache_status": (lambda a: query_store("status", a), "查看 Gmail 本地镜像的账号、覆盖范围及最后同步时间；不联网。", {}),
    "get_gmail_inbox_todo": (lambda a: query_store("todo", a),
                             "邮件待办：最近几天收件箱里还没读的学校邮件，已去掉推广和社交类；用户问“邮箱里有什么要处理的”先用这个。"
                             "只读本地镜像，as_of 是上次同步时间。",
                             {"days": ("integer", False), "limit": ("integer", False)}),
    "search_gmail_cache": (lambda a: query_store("search", a),
                           "离线全文搜索已下载邮件正文；query 是普通关键词，不是 Gmail 搜索语法。每条带日期、未读、在收件箱、星标、重要、"
                           "分类和附件数，以及来源链接和缓存覆盖范围。",
                           {"query": ("string", False), "limit": ("integer", False), "offset": ("integer", False)}),
    "read_gmail_cached_thread": (lambda a: query_store("thread", a), "读取已下载邮件对话，支持字符分页与附件元数据；同步范围外的消息可能未下载。",
                                 {"thread_id": ("string", True), "offset": ("integer", False), "max_chars": ("integer", False)}),
}
