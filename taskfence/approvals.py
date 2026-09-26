"""Human approval store (spec §16; DECISIONS §9; kit Prompts E1, E2).

SQLite-backed so the Streamlit dashboard — a separate process — can list
pending approvals. Exposes only create/get/pending/set_status: no update of
approval content, no delete. Resolution itself is audited by the gateway.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("taskfence.sqlite3")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS approvals ("
    " approval_id TEXT PRIMARY KEY,"
    " task_id TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " args TEXT NOT NULL,"
    " reasons TEXT NOT NULL,"
    " status TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " resolved_at TEXT)"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        with self.conn:
            self.conn.execute(_SCHEMA)

    def create(self, task_id: str, tool: str, args: dict,
               reasons: list[str]) -> str:
        approval_id = f"apr-{uuid.uuid4().hex[:12]}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO approvals (approval_id, task_id, tool, args,"
                " reasons, status, created_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (approval_id, task_id, tool, json.dumps(args),
                 " || ".join(reasons), _now()))
        return approval_id

    def get(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT approval_id, task_id, tool, args, reasons, status,"
            " created_at, resolved_at FROM approvals WHERE approval_id = ?",
            (approval_id,)).fetchone()
        if row is None:
            return None
        names = ["approval_id", "task_id", "tool", "args", "reasons",
                 "status", "created_at", "resolved_at"]
        record = dict(zip(names, row))
        record["args"] = json.loads(record["args"])
        return record

    def pending(self, task_id: str | None = None) -> list[dict]:
        if task_id is None:
            rows = self.conn.execute(
                "SELECT approval_id, task_id, tool, reasons, created_at"
                " FROM approvals WHERE status = 'pending' ORDER BY rowid")
        else:
            rows = self.conn.execute(
                "SELECT approval_id, task_id, tool, reasons, created_at"
                " FROM approvals WHERE status = 'pending' AND task_id = ?"
                " ORDER BY rowid", (task_id,))
        return [dict(zip(["approval_id", "task_id", "tool", "reasons",
                          "created_at"], row)) for row in rows.fetchall()]

    def set_status(self, approval_id: str, status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, resolved_at = ?"
                " WHERE approval_id = ?",
                (status, _now(), approval_id))
        return cur.rowcount == 1

    def close(self) -> None:
        self.conn.close()
