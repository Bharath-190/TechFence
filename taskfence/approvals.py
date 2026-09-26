"""Human approval store (spec §16; DECISIONS §9; kit Prompts E1, E2; FIX1).

SQLite-backed so the Streamlit dashboard — a separate process — can list
pending approvals. Exposes only create/get/pending/set_status: no update of
approval content, no delete. Resolution itself is audited by the gateway.

FIX1 (approval record integrity):
- The record stores the EXACT original tool-call arguments (`tool_args`),
  separate from the FlowRequest policy snapshot used for evaluation/display.
  The snapshot's payload may be truncated; replay must never use it.
- The record is bound to the reviewed contract: `contract_id` +
  `contract_version` plus a frozen `contract_snapshot` (TaskContract dump),
  because the repository keeps no historical contract store — resolution
  verifies against the snapshot and fails closed if it is gone or mismatched.
- `declared_source_assets` records the ORIGINAL declared sources of the
  request, so scope expansion grants only what was declared (never
  session-taint groups).
- Full tool_args are admin-only sensitive state (DECISIONS §10): `pending()`
  keeps its narrow five-column projection and must never expose them.
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
    " resolved_at TEXT NULL)"
)

# Narrow projection for unauthenticated listing (GET /approvals, task state).
# Do NOT widen: these rows leave the admin boundary.
_PENDING_COLUMNS = ("approval_id", "task_id", "tool", "reasons", "created_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        with self.conn:
            self.conn.execute(_SCHEMA)

    def create(self, task_id: str, tool: str, request_snapshot: dict,
               tool_args: dict, contract, declared_source_assets: list[str],
               declared_source_groups: list[str],
               reasons: list[str]) -> str:
        """Create a pending approval.

        request_snapshot: FlowRequest model_dump() — the policy/eval snapshot
          (payload may be truncated; never used for replay).
        tool_args: the EXACT original tool-call arguments (full payload).
        contract: the reviewed TaskContract (id + version + frozen snapshot).
        declared_source_assets: the request's declared source asset ids.
        declared_source_groups: gateway-resolved groups of the DECLARED
          assets only (display/expand scope; never session-taint groups).
        """
        approval_id = f"apr-{uuid.uuid4().hex[:12]}"
        args_payload = {
            "request": request_snapshot,
            "tool_args": tool_args,
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "contract_snapshot": contract.model_dump(),
            "declared_source_assets": sorted(declared_source_assets),
            "declared_source_groups": sorted(declared_source_groups),
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO approvals (approval_id, task_id, tool, args,"
                " reasons, status, created_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (approval_id, task_id, tool, json.dumps(args_payload),
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
        record.update(self._parse_args(json.loads(record["args"])))
        return record

    @staticmethod
    def _parse_args(raw: dict) -> dict:
        """Normalize an approval's args payload.

        FIX1 records carry `request` (policy snapshot), `tool_args`, the
        reviewed contract id/version/snapshot, and declared source assets.
        Pre-FIX1 rows store a bare FlowRequest dump; they are surfaced as a
        request-only record without binding data, so resolution fails closed
        (kit FIX1-A: never silently fall back).
        """
        if "request" in raw:
            tool_args = raw.get("tool_args")
            return {
                "request": raw.get("request") or {},
                "tool_args": tool_args if isinstance(tool_args, dict) else None,
                "contract_id": raw.get("contract_id", ""),
                "contract_version": raw.get("contract_version", ""),
                "contract_snapshot": raw.get("contract_snapshot"),
                "declared_source_assets": list(
                    raw.get("declared_source_assets") or []),
                "declared_source_groups": list(
                    raw.get("declared_source_groups") or []),
            }
        # Legacy row: treat the whole payload as the request snapshot.
        return {
            "request": raw,
            "tool_args": None,
            "contract_id": "",
            "contract_version": "",
            "contract_snapshot": None,
            "declared_source_assets": [],
            "declared_source_groups": [],
        }

    def pending(self, task_id: str | None = None) -> list[dict]:
        if task_id is None:
            rows = self.conn.execute(
                f"SELECT {', '.join(_PENDING_COLUMNS)} FROM approvals"
                " WHERE status = 'pending' ORDER BY rowid")
        else:
            rows = self.conn.execute(
                f"SELECT {', '.join(_PENDING_COLUMNS)} FROM approvals"
                " WHERE status = 'pending' AND task_id = ?"
                " ORDER BY rowid", (task_id,))
        return [dict(zip(_PENDING_COLUMNS, row)) for row in rows.fetchall()]

    def set_status(self, approval_id: str, status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE approvals SET status = ?, resolved_at = ?"
                " WHERE approval_id = ?",
                (status, _now(), approval_id))
        return cur.rowcount == 1

    def close(self) -> None:
        self.conn.close()
