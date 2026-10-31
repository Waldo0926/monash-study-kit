"""Durable, payload-bound review tickets for optional upstream writes."""

import hashlib
import json
import os
import secrets
import sqlite3
import time

from .paths import DATA_DIR, ensure_private_dir

RETENTION = 30 * 86400  # finished tickets and jobs older than this are deleted


class Rejected(ValueError):
    """A check failed before anything was sent upstream, so the action is safely not done."""


def connect():
    directory = DATA_DIR / "student-services"
    ensure_private_dir(directory)
    filename = directory / "actions.db"
    fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    con = sqlite3.connect(filename, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
        digest TEXT NOT NULL, expires INTEGER NOT NULL, state TEXT NOT NULL, result TEXT)""")
    return con


def prepare(owner, kind, payload):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    action_id = secrets.token_urlsafe(24)
    con = connect()
    try:
        # Tickets still 'executing' or 'unknown' are kept: they record a write whose outcome needs checking.
        con.execute(
            "DELETE FROM actions WHERE expires<? AND state IN ('pending','done','rejected')",
            (int(time.time()) - RETENTION,),
        )
        con.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,?,NULL)",
            (action_id, owner, kind, body, digest, int(time.time()) + 600, "pending"),
        )
        con.commit()
    finally:
        con.close()
    return {
        "action_id": action_id,
        "review_digest": digest,
        "expires_in": 600,
        "action": kind,
        "review": {key: value for key, value in payload.items() if key != "raw"},
        "instruction": "Show the complete review to the user. Execute only after explicit confirmation of this exact action.",
    }


def execute(owner, action_id, digest, user_confirmed, perform):
    if user_confirmed is not True:
        raise ValueError("Explicit user confirmation is required")
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM actions WHERE id=? AND owner=?", (action_id, owner)).fetchone()
        if not row or row["expires"] < time.time() or not secrets.compare_digest(row["digest"], digest):
            raise ValueError("Review expired or does not match this client and payload; prepare again")
        if row["state"] != "pending":
            return {
                "state": row["state"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "instruction": "Do not retry an ambiguous upstream write; verify its actual state first.",
            }
        con.execute("UPDATE actions SET state='executing' WHERE id=?", (action_id,))
        con.commit()  # Claim before contacting the upstream; concurrent calls cannot duplicate a send.
        try:
            result = perform(row["kind"], json.loads(row["payload"]))
        except Rejected as error:
            con.execute(
                "UPDATE actions SET state='rejected',result=? WHERE id=?",
                (json.dumps({"reason": str(error)}), action_id),
            )
            con.commit()
            raise
        except Exception:
            con.execute("UPDATE actions SET state='unknown' WHERE id=?", (action_id,))
            con.commit()
            raise
        con.execute("UPDATE actions SET state='done',result=? WHERE id=?", (json.dumps(result), action_id))
        con.commit()
        return {"state": "done", "result": result}
    finally:
        con.close()
