"""Human approval store (Phase 2; FIX1; AD-25).

Durable, backed by the single configured database. Resolution is an **atomic
conditional claim**: exactly one resolver can move a record out of ``pending``,
and the loser gets ``False`` rather than executing anything.

FIX1 record integrity is unchanged:
- the exact original ``tool_args`` are stored separately from the truncated
  policy snapshot;
- the reviewed contract id/version/snapshot are stored with the record;
- declared source assets are stored so scope expansion grants only what was
  declared, never session taint.
"""

from __future__ import annotations

import json
import uuid

from taskfence.persistence import Database, coerce_database, now_iso

# Narrow projection for unauthenticated listing. Do NOT widen.
_PENDING_COLUMNS = ("approval_id", "task_id", "tool", "reasons", "created_at")

# The only states a resolution claim may move a record to (F4). Anything
# else is a programming error, not a status write: there is deliberately no
# unconditional setter, so no caller can bypass the pending-state claim.
RESOLUTION_STATUSES = ("allowed_once", "denied", "expanded")


class ApprovalStore:
    def close(self) -> None:
        """Legacy lifecycle hook: connections are per operation now."""
        return None
    def __init__(self, db: Database | str | None = None):
        self.db = coerce_database(db)

    def create(self, task_id: str, tool: str, request_snapshot: dict,
               tool_args: dict, contract, declared_source_assets: list[str],
               declared_source_groups: list[str],
               reasons: list[str], *, tool_binding: dict | None = None,
               asset_binding: dict | None = None) -> str:
        """Create a pending approval.

        ``tool_binding`` records the tool identity/action/destination and the
        connector configuration the human reviewed (R1b); ``asset_binding``
        records the content digest and classification of an approved read
        (R3). Both are verified at replay and absent on pre-R1/R3 records,
        where replay fails closed and asks for re-review.
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
            "tool_binding": tool_binding,
            "asset_binding": asset_binding,
        }
        self.db.execute(
            "INSERT INTO approvals (approval_id, task_id, tool, args,"
            " reasons, status, created_at, resolved_at, resolved_by)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (approval_id, task_id, tool, json.dumps(args_payload),
             " || ".join(reasons), now_iso()))
        return approval_id

    def get(self, approval_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT approval_id, task_id, tool, args, reasons, status,"
            " created_at, resolved_at, resolved_by FROM approvals"
            " WHERE approval_id = ?", (approval_id,))
        if row is None:
            return None
        row.update(self._parse_args(json.loads(row["args"])))
        return row

    @staticmethod
    def _parse_args(raw: dict) -> dict:
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
                "tool_binding": raw.get("tool_binding"),
                "asset_binding": raw.get("asset_binding"),
            }
        return {
            "request": raw,
            "tool_args": None,
            "tool_binding": None,
            "asset_binding": None,
            "contract_id": "",
            "contract_version": "",
            "contract_snapshot": None,
            "declared_source_assets": [],
            "declared_source_groups": [],
        }

    def pending(self, task_id: str | None = None) -> list[dict]:
        columns = ", ".join(_PENDING_COLUMNS)
        if task_id is None:
            return self.db.query(
                f"SELECT {columns} FROM approvals"
                " WHERE status = 'pending' ORDER BY created_at, approval_id")
        return self.db.query(
            f"SELECT {columns} FROM approvals"
            " WHERE status = 'pending' AND task_id = ?"
            " ORDER BY created_at, approval_id", (task_id,))

    def claim(self, approval_id: str, status: str = "allowed_once",
              resolved_by: str | None = None) -> bool:
        """Atomically move a pending approval to ``status``.

        Returns True for the single winner, False for every other attempt
        (lost race, unknown id, or already resolved). Conditional on the
        current status, so two resolvers cannot both succeed. ``status``
        must be a resolution outcome; an unknown value raises before any
        write, so a claim can never be used as a generic status setter.
        """
        if status not in RESOLUTION_STATUSES:
            raise ValueError(
                f"approval claim status must be one of {RESOLUTION_STATUSES},"
                f" got {status!r}")
        changed = self.db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?,"
            " resolved_by = ? WHERE approval_id = ? AND status = 'pending'",
            (status, now_iso(), resolved_by, approval_id))
        return changed == 1
