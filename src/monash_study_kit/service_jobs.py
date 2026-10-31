"""Owner-scoped background jobs so sync/OCR never occupies a remote MCP request."""
import concurrent.futures
import json
import secrets
import threading
import time

from .service_actions import connect

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix="student-services")
_lock = threading.Lock()
_started = False


def database():
    global _started
    con = connect()
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,owner TEXT,kind TEXT,args TEXT,state TEXT,created INTEGER,result TEXT,
        scopes TEXT NOT NULL DEFAULT '[]')""")
    if "scopes" not in {row[1] for row in con.execute("PRAGMA table_info(jobs)")}:
        con.execute("ALTER TABLE jobs ADD COLUMN scopes TEXT NOT NULL DEFAULT '[]'")
    with _lock:
        if not _started:
            con.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running')")
            _started = True
    con.commit()
    return con


def submit(owner, kind, arguments, work, required_scopes=()):
    body = json.dumps(arguments,sort_keys=True)
    permissions=json.dumps(sorted(required_scopes))
    con = database()
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute("SELECT id FROM jobs WHERE owner=? AND kind=? AND args=? AND scopes=? AND state IN ('queued','running')",(owner,kind,body,permissions)).fetchone()
        if active:
            return {"job_id":active[0],"state":"already_running"}
        job_id = secrets.token_urlsafe(18)
        con.execute("INSERT INTO jobs (id,owner,kind,args,state,created,result,scopes) VALUES (?,?,?,?,?,?,NULL,?)",(job_id,owner,kind,body,"queued",int(time.time()),permissions))
        con.commit()
    finally:
        con.close()
    def run():
        conn = database()
        try:
            conn.execute("UPDATE jobs SET state='running' WHERE id=?",(job_id,))
            conn.commit()
            try:
                result = work()
                state = "done"
            except Exception as error:
                result,state = {"error":type(error).__name__,"message":str(error)[:500]},"failed"
            conn.execute("UPDATE jobs SET state=?,result=? WHERE id=?",(state,json.dumps(result),job_id))
            conn.commit()
        finally:
            conn.close()
    _pool.submit(run)
    return {"job_id":job_id,"state":"queued","instruction":"Use student_job_status to read progress and result"}


def status(owner, job_id):
    con = database()
    try:
        row = con.execute("SELECT id,kind,state,created,result,scopes FROM jobs WHERE id=? AND owner=?",(job_id,owner)).fetchone()
        if not row:
            raise LookupError("Job not found for this client")
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["required_scopes"] = json.loads(result.pop("scopes"))
        return result
    finally:
        con.close()
