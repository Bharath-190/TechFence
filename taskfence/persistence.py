"""Durable shared persistence (Phase 2; AD-02, AD-03, AD-04, AD-25).

One configured backend serves the gateway, audit, approvals, lineage and the
dashboard. The backend is PostgreSQL when ``TASKFENCE_DATABASE_URL`` is set and
SQLite otherwise; there is no fallback from one to the other at runtime.

Design rules enforced here:

- Explicit SQL only, no ORM. Statements are written with ``?`` placeholders and
  translated for PostgreSQL.
- Schema initialization is versioned and forward-only; running it twice is a
  no-op and never drops data.
- ``task_decisions`` is append-only: this module exposes append and read only.
- No in-memory dictionary is authoritative for task state.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1

SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version INTEGER PRIMARY KEY,"
    " applied_at TEXT NOT NULL)",
)

_TABLES_SQLITE = (
    "CREATE TABLE IF NOT EXISTS tasks ("
    " task_id TEXT PRIMARY KEY,"
    " task_text TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " current_contract_version TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS task_contracts ("
    " task_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " contract_json TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " PRIMARY KEY (task_id, contract_version))",
    "CREATE TABLE IF NOT EXISTS task_decisions ("
    " decision_id TEXT PRIMARY KEY,"
    " seq INTEGER NOT NULL,"
    " task_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " destination TEXT NOT NULL,"
    " outcome TEXT NOT NULL,"
    " reasons_json TEXT NOT NULL,"
    " request_fingerprint TEXT NOT NULL,"
    " created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ix_task_decisions_task"
    " ON task_decisions (task_id, created_at)",
    "CREATE TABLE IF NOT EXISTS task_reads ("
    " task_id TEXT NOT NULL,"
    " asset_id TEXT NOT NULL,"
    " first_seen_at TEXT NOT NULL,"
    " PRIMARY KEY (task_id, asset_id))",
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
    " lineage_path TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS approvals ("
    " approval_id TEXT PRIMARY KEY,"
    " task_id TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " args TEXT NOT NULL,"
    " reasons TEXT NOT NULL,"
    " status TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " resolved_at TEXT NULL,"
    " resolved_by TEXT NULL)",
    "CREATE TABLE IF NOT EXISTS task_state ("
    " task_id TEXT PRIMARY KEY,"
    " updated_at TEXT NOT NULL,"
    " state TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS lineage_nodes ("
    " task_id TEXT NOT NULL,"
    " node TEXT NOT NULL,"
    " labels TEXT NOT NULL,"
    " PRIMARY KEY (task_id, node))",
    "CREATE TABLE IF NOT EXISTS lineage_edges ("
    " task_id TEXT NOT NULL,"
    " src TEXT NOT NULL,"
    " dst TEXT NOT NULL,"
    " transformation TEXT NOT NULL,"
    " PRIMARY KEY (task_id, src, dst))",
)

# PostgreSQL needs an identity column where SQLite uses AUTOINCREMENT, and a
# plain BIGINT for the decision append sequence (supplied by the writer).
_TASK_DECISIONS_POSTGRES = (
    "CREATE TABLE IF NOT EXISTS task_decisions ("
    " decision_id TEXT PRIMARY KEY,"
    " seq BIGINT NOT NULL,"
    " task_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " tool TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " destination TEXT NOT NULL,"
    " outcome TEXT NOT NULL,"
    " reasons_json TEXT NOT NULL,"
    " request_fingerprint TEXT NOT NULL,"
    " created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ix_task_decisions_task"
    " ON task_decisions (task_id, created_at)",
)

# PostgreSQL needs an identity column where SQLite uses AUTOINCREMENT.
_AUDIT_EVENTS_POSTGRES = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    " id BIGSERIAL PRIMARY KEY,"
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
    " lineage_path TEXT NOT NULL)",
)


# Reserved columns designed up front and added forward-only (AD-03, G-23).
# Each is NULL until its owning phase starts writing it:
#   tenant/org + authenticated subject -> Phase 3 (identity)
#   agent/session id, policy version, trace id -> Phase 9 / Phase 4
#   idempotency key / request hash -> Phase 10 (reconciliation, G-11)
#   approval actor (approvals.resolved_by) + authorization basis -> Phase 3/4
#   resource/destination ids -> Phase 5 (granularity)
#   kill-switch state, reconciliation state -> Phase 10
_RESERVED_COLUMNS = (
    ("tasks", "subject", "TEXT"),
    ("audit_events", "tenant_id", "TEXT"),
    ("audit_events", "subject", "TEXT"),
    ("audit_events", "agent_id", "TEXT"),
    ("audit_events", "session_id", "TEXT"),
    ("audit_events", "policy_version", "TEXT"),
    ("audit_events", "trace_id", "TEXT"),
    ("audit_events", "idempotency_key", "TEXT"),
    ("audit_events", "approval_actor", "TEXT"),
    ("audit_events", "authorization_basis", "TEXT"),
    ("audit_events", "resource_id", "TEXT"),
    ("audit_events", "kill_switch_state", "TEXT"),
    ("audit_events", "reconciliation_state", "TEXT"),
    ("approvals", "idempotency_key", "TEXT"),
    ("approvals", "authorization_basis", "TEXT"),
    ("approvals", "resource_id", "TEXT"),
    ("approvals", "reconciliation_state", "TEXT"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """One backend, explicit SQL, per-operation connections."""

    def __init__(self, config) -> None:
        self.backend = "postgres" if config.use_postgres else "sqlite"
        self._dsn = config.database_url.reveal() if config.use_postgres else None
        self.sqlite_path = Path(config.sqlite_path)
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazily apply the schema so constructing a Database never
        connects: a configured-but-unreachable backend fails at first use
        (preflight), not at import time."""
        if not self._initialized:
            self.initialize()

    # -- connection ---------------------------------------------------------
    def _connect(self):
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(self._dsn, row_factory=dict_row)
        conn = sqlite3.connect(self.sqlite_path, timeout=30,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connect(self) -> Iterator[Any]:
        self._ensure_initialized()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Commit on success, roll back on any exception, then close."""
        self._ensure_initialized()
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    # -- helpers ------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as conn:
            return conn.execute(self._sql(sql), params).rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(self._sql(sql), params).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- schema -------------------------------------------------------------
    def initialize(self) -> None:
        """Create or upgrade the schema. Idempotent and forward-only."""
        if self._initialized:
            return
        conn = self._connect()
        try:
            with conn:
                for statement in SCHEMA_MIGRATIONS:
                    conn.execute(statement)
                statements = list(_TABLES_SQLITE)
                if self.backend == "postgres":
                    statements = [
                        s for s in statements
                        if "audit_events" not in s and "task_decisions" not in s
                    ]
                    statements.extend(_AUDIT_EVENTS_POSTGRES)
                    statements.extend(_TASK_DECISIONS_POSTGRES)
                for statement in statements:
                    conn.execute(statement)
                self._ensure_column(conn, "approvals", "resolved_by", "TEXT")
                # Phase 2 pre-release upgrade: add the append sequence to a
                # table created before it existed (legacy rows read as 0).
                self._ensure_column(
                    conn, "task_decisions", "seq",
                    "BIGINT" if self.backend == "postgres" else "INTEGER")
                for table, column, ddl in _RESERVED_COLUMNS:
                    self._ensure_column(conn, table, column, ddl)
                conn.execute(
                    self._sql(
                        "INSERT INTO schema_migrations (version, applied_at)"
                        " VALUES (?, ?) ON CONFLICT (version) DO NOTHING"),
                    (SCHEMA_VERSION, now_iso()))
            self._initialized = True
        finally:
            conn.close()

    def _ensure_column(self, conn, table: str, column: str, ddl: str) -> None:
        if self.backend == "postgres":
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = %s",
                (table, column)).fetchone()
        else:
            row = None
            for info in conn.execute(f"PRAGMA table_info({table})"):
                if info[1] == column:
                    row = (1,)
                    break
        if row is None:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def coerce_database(value: Any = None) -> Database:
    """Accept a Database or a SQLite path (legacy constructor call sites).

    A path always means a local SQLite database; it is never used to imply a
    different backend than the configured one in production paths.
    """
    if isinstance(value, Database):
        return value
    from taskfence.config import Config
    path = str(value) if value is not None else "taskfence.sqlite3"
    return Database(Config.from_env({"TASKFENCE_DB": path}))


class TaskStore:
    """Authoritative task, contract and append-only decision state."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- tasks / contracts --------------------------------------------------
    def create(self, task_id: str, task_text: str, contract) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                self.db._sql(
                    "INSERT INTO tasks (task_id, task_text, created_at,"
                    " current_contract_version) VALUES (?, ?, ?, ?)"),
                (task_id, task_text, now_iso(), contract.version))
            conn.execute(
                self.db._sql(
                    "INSERT INTO task_contracts (task_id, contract_version,"
                    " contract_json, created_at) VALUES (?, ?, ?, ?)"),
                (task_id, contract.version, contract.model_dump_json(),
                 now_iso()))

    def exists(self, task_id: str) -> bool:
        return self.db.query_one(
            "SELECT 1 AS ok FROM tasks WHERE task_id = ?", (task_id,)) is not None

    def task_text(self, task_id: str) -> str:
        row = self.db.query_one(
            "SELECT task_text FROM tasks WHERE task_id = ?", (task_id,))
        return row["task_text"] if row else ""

    def current_version(self, task_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT current_contract_version FROM tasks WHERE task_id = ?",
            (task_id,))
        return row["current_contract_version"] if row else None

    def get_contract(self, task_id: str):
        """Return the current TaskContract, or None when the task is unknown."""
        from taskfence.models import TaskContract

        row = self.db.query_one(
            "SELECT c.contract_json FROM tasks t"
            " JOIN task_contracts c"
            "   ON c.task_id = t.task_id"
            "  AND c.contract_version = t.current_contract_version"
            " WHERE t.task_id = ?", (task_id,))
        if row is None:
            return None
        return TaskContract.model_validate_json(row["contract_json"])

    def set_contract(self, task_id: str, contract) -> None:
        """Append a new contract version and advance the pointer atomically."""
        with self.db.transaction() as conn:
            conn.execute(
                self.db._sql(
                    "INSERT INTO task_contracts (task_id, contract_version,"
                    " contract_json, created_at) VALUES (?, ?, ?, ?)"),
                (task_id, contract.version, contract.model_dump_json(), now_iso()))
            conn.execute(
                self.db._sql(
                    "UPDATE tasks SET current_contract_version = ?"
                    " WHERE task_id = ?"),
                (contract.version, task_id))

    # -- append-only decisions ---------------------------------------------
    def append_decision(self, task_id: str, contract_version: str, tool: str,
                        action: str, destination: str, outcome: str,
                        reasons: Sequence[str], request_fingerprint: str) -> str:
        """Append one immutable decision row (the only write path).

        `seq` is assigned inside the same transaction from the current
        maximum, so "latest decision" is a deterministic query rather than a
        timestamp tie-break. PostgreSQL takes an exclusive table lock first so
        concurrent appenders serialize instead of racing on the maximum;
        SQLite serializes writers itself.
        """
        decision_id = f"dec-{uuid.uuid4().hex}"
        import json
        with self.db.transaction() as conn:
            if self.db.backend == "postgres":
                conn.execute("LOCK TABLE task_decisions IN EXCLUSIVE MODE")
            conn.execute(
                self.db._sql(
                    "INSERT INTO task_decisions (decision_id, task_id,"
                    " contract_version, tool, action, destination, outcome,"
                    " reasons_json, request_fingerprint, created_at, seq)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " (SELECT COALESCE(MAX(seq), 0) + 1"
                    "   FROM task_decisions))"),
                (decision_id, task_id, contract_version, tool, action,
                 destination, outcome, json.dumps(list(reasons)),
                 request_fingerprint, now_iso()))
        return decision_id

    def latest_decision(self, task_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM task_decisions WHERE task_id = ?"
            " ORDER BY COALESCE(seq, 0) DESC, decision_id DESC LIMIT 1",
            (task_id,))
        if row is None:
            return None
        import json
        row["reasons"] = json.loads(row.pop("reasons_json"))
        return row

    def decisions(self, task_id: str) -> list[dict]:
        import json
        rows = self.db.query(
            "SELECT * FROM task_decisions WHERE task_id = ?"
            " ORDER BY COALESCE(seq, 0), decision_id", (task_id,))
        for row in rows:
            row["reasons"] = json.loads(row.pop("reasons_json"))
        return rows

    def count_decisions(self, task_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM task_decisions WHERE task_id = ?",
            (task_id,))
        return int(row["n"]) if row else 0


class Persistence:
    """One authoritative backend, shared by every store (AD-21/AD-25, G-16).

    The gateway, the dashboard, the scenario harness and tests all build
    their stores from a single `Persistence` instance created from one
    `Config` snapshot. There is no second backend and no silent fallback:
    a configured PostgreSQL backend is never replaced by SQLite.
    """

    def __init__(self, config=None) -> None:
        from taskfence.approvals import ApprovalStore
        from taskfence.audit import AuditLog, TaskStateStore
        from taskfence.config import load_config
        from taskfence.lineage import LineageTracker

        self.config = load_config() if config is None else config
        self.db = Database(self.config)
        self.tasks = TaskStore(self.db)
        self.audit = AuditLog(self.db)
        self.state = TaskStateStore(self.db)
        self.approvals = ApprovalStore(self.db)
        self.lineage = LineageTracker(self.db)

    def preflight(self) -> None:
        """Touch the configured backend once (fails fast if unreachable)."""
        self.db.query_one("SELECT 1 AS ok")
