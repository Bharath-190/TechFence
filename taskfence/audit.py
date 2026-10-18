"""Append-only audit trail and display-state projection (Phase 2).

Both stores are backed by the single configured ``Database`` (AD-21/AD-25);
they no longer open their own SQLite file. ``AuditLog`` exposes only append and
read operations. ``TaskStateStore`` remains a dashboard projection: it is
never read by an authorization decision.
"""

from __future__ import annotations

import json

from taskfence.persistence import Database, coerce_database, now_iso


class AuditLog:
    def close(self) -> None:
        """Legacy lifecycle hook: connections are per operation now."""
        return None
    def __init__(self, db: Database | str | None = None, db_path=None):
        self.db = coerce_database(db if db is not None else db_path)

    def log_event(self, event: dict) -> int:
        """Append one audit event; returns nothing meaningful for callers.

        The timestamp is added here — the only place wall-clock time is
        allowed for audit records.

        Phase 3 (G-04): an authenticated reviewer's subject is written to the
        dedicated reserved `subject` column, never into `reasons`. `reasons`
        stays policy-derived so consumers can trust it as policy output, and
        the identity value can be withheld from unauthenticated readers
        independently of the decision trail.
        """
        with self.db.transaction() as conn:
            conn.execute(
                self.db._sql(
                    "INSERT INTO audit_events (timestamp, task_id, task_text,"
                    " contract_id, contract_version, tool, action, sources,"
                    " labels, transformation, destination, decision, reasons,"
                    " lineage_path, subject)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                (event.get("timestamp") or now_iso(),
                 event["task_id"], event.get("task_text", ""),
                 event.get("contract_id", ""),
                 event.get("contract_version", ""),
                 event["tool"], event["action"],
                 ",".join(event.get("sources", [])),
                 ",".join(sorted(event.get("labels", []))),
                 event.get("transformation", ""),
                 event["destination"], event["decision"],
                 " || ".join(event.get("reasons", [])),
                 event.get("lineage_path", ""),
                 event.get("subject")))
            row = conn.execute(
                "SELECT lastval()" if self.db.backend == "postgres"
                else "SELECT last_insert_rowid()").fetchone()
            if row is None:
                return 0
            key = "lastval" if self.db.backend == "postgres" else 0
            return int(row[key])

    def read_all(self, task_id: str | None = None) -> list[dict]:
        if task_id is None:
            return self.db.query("SELECT * FROM audit_events ORDER BY id")
        return self.db.query(
            "SELECT * FROM audit_events WHERE task_id = ? ORDER BY id",
            (task_id,))

    def count(self, task_id: str | None = None) -> int:
        if task_id is None:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM audit_events")
        else:
            row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM audit_events WHERE task_id = ?",
                (task_id,))
        return int(row["n"]) if row else 0


class TaskStateStore:
    def close(self) -> None:
        """Legacy lifecycle hook: connections are per operation now."""
        return None
    """Latest live task state, persisted so the dashboard can render it.

    Display data only: no authorization decision may read this table.
    """

    def __init__(self, db: Database | str | None = None, db_path=None):
        self.db = coerce_database(db if db is not None else db_path)

    def save(self, task_id: str, state: dict) -> None:
        self.db.execute(
            "INSERT INTO task_state (task_id, updated_at, state)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(task_id) DO UPDATE SET"
            " updated_at=excluded.updated_at, state=excluded.state",
            (task_id, now_iso(), json.dumps(state)))

    def get(self, task_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT state FROM task_state WHERE task_id = ?", (task_id,))
        return json.loads(row["state"]) if row else None

    def latest_task_id(self) -> str | None:
        row = self.db.query_one(
            "SELECT task_id FROM task_state ORDER BY updated_at DESC LIMIT 1")
        return row["task_id"] if row else None

    def all_tasks(self) -> list[str]:
        rows = self.db.query(
            "SELECT task_id FROM task_state ORDER BY updated_at DESC")
        return [row["task_id"] for row in rows]

    def clear(self) -> None:
        self.db.execute("DELETE FROM task_state")
