"""Append-only audit trail (spec §29; kit Prompt B4).

SQLite table audit_events. The module exposes ONLY log_event() and read
functions — no update, no delete (tests assert this via inspect).
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("taskfence.sqlite3")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " timestamp TEXT NOT NULL,"
    " task_id TEXT NOT NULL,"
    " task_text TEXT NOT NULL,"
    " contract_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " sources TEXT NOT NULL,"
    " labels TEXT NOT NULL,"
    " transformation TEXT NOT NULL,"
    " destination TEXT NOT NULL,"
    " decision TEXT NOT NULL,"
    " reasons TEXT NOT NULL,"
    " lineage_path TEXT NOT NULL)"
)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.conn = _connect(db_path)
        with self.conn:
            self.conn.execute(_SCHEMA)

    def log_event(self, event: dict) -> int:
        """Append one audit event; returns its row id. Timestamp is added
        here — the only place wall-clock time is allowed (kit guardrail 9)."""
        row = (
            event.get("timestamp") or _now_iso(),
            event["task_id"],
            event.get("task_text", ""),
            event.get("contract_id", ""),
            event.get("contract_version", ""),
            event["tool"],
            event["action"],
            ",".join(event.get("sources", [])),
            ",".join(sorted(event.get("labels", []))),
            event.get("transformation", ""),
            event["destination"],
            event["decision"],
            " || ".join(event.get("reasons", [])),
            event.get("lineage_path", ""),
        )
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO audit_events (timestamp, task_id, task_text,"
                " contract_id, contract_version, tool, action, sources,"
                " labels, transformation, destination, decision, reasons,"
                " lineage_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
            return cur.lastrowid

    def read_all(self, task_id: str | None = None) -> list[dict]:
        if task_id is None:
            rows = self.conn.execute(
                "SELECT * FROM audit_events ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_events WHERE task_id = ? ORDER BY id",
                (task_id,))
        names = [d[0] for d in rows.description]
        return [dict(zip(names, row)) for row in rows.fetchall()]

    def count(self, task_id: str | None = None) -> int:
        if task_id is None:
            row = self.conn.execute("SELECT COUNT(*) FROM audit_events")
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE task_id = ?",
                (task_id,))
        return row.fetchone()[0]

    def close(self) -> None:
        self.conn.close()


class TaskStateStore:
    """Latest live task state, persisted so the Streamlit dashboard (a
    separate process) can render it. Display data only — the dashboard
    computes nothing about security (kit Prompt F3)."""

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS task_state ("
        " task_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL,"
        " state TEXT NOT NULL)"
    )

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.conn = _connect(db_path)
        with self.conn:
            self.conn.execute(self._SCHEMA)

    def save(self, task_id: str, state: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO task_state (task_id, updated_at, state)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                " updated_at=excluded.updated_at, state=excluded.state",
                (task_id, _now_iso(), json.dumps(state)))

    def get(self, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT state FROM task_state WHERE task_id = ?",
            (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_task_id(self) -> str | None:
        row = conn_execute_latest(self.conn)
        return row

    def all_tasks(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT task_id FROM task_state ORDER BY updated_at DESC")
        return [row[0] for row in rows.fetchall()]

    def clear(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM task_state")

    def close(self) -> None:
        self.conn.close()


def conn_execute_latest(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT task_id FROM task_state ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None
