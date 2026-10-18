"""Fresh-interpreter worker for the Phase 2 cross-process tests (G-01/G-02/G-03).

Every invocation starts a brand-new Python process with no shared memory with
its parent or its siblings, so the only way it can see state is the configured
database. The backend is selected entirely by the environment the parent
passes in (`TASKFENCE_DB` for SQLite, `TASKFENCE_DATABASE_URL` for
PostgreSQL).

Usage: python tests/proc_child.py <action> [args...]
Prints one line prefixed with ``CHILD_JSON:`` containing the JSON result.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _prepare() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    os.environ.setdefault("TASKFENCE_OLLAMA_URL", "http://127.0.0.1:1")
    os.environ.setdefault("TASKFENCE_ADMIN_TOKEN", "devtoken")
    from taskfence import registry
    outbox = os.environ.get("TF_OUTBOX_DIR")
    if outbox:
        registry.OUTBOX_DIR = Path(outbox)
        registry.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)


def _json(response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _client():
    _prepare()
    from fastapi.testclient import TestClient
    from taskfence.gateway import app
    return TestClient(app)


def main(argv: list[str]) -> int:
    action, rest = argv[0], argv[1:]
    client = _client()

    if action == "create_task":
        text = rest[0]
        response = client.post("/tasks", json={"task_text": text})
        body = _json(response)
        payload = {"http": response.status_code,
                   "task_id": body.get("task_id"),
                   "contract_version": (body.get("contract") or {}).get(
                       "version"),
                   "detail": body.get("detail")}

    elif action == "tool_call":
        task_id, tool, args_json = rest[0], rest[1], rest[2]
        response = client.post(
            f"/tasks/{task_id}/tool-call",
            json={"tool": tool, "args": json.loads(args_json)})
        body = _json(response)
        decision = body.get("decision") or {}
        payload = {"http": response.status_code,
                   "outcome": decision.get("outcome"),
                   "tainted": body.get("tainted", False),
                   "approval_id": body.get("approval_id"),
                   "result": body.get("result"),
                   "lineage_path": body.get("lineage_path"),
                   "detail": body.get("detail") or body.get("agent_message")}

    elif action == "resolve":
        approval_id, choice = rest[0], rest[1]
        response = client.post(
            f"/approvals/{approval_id}/resolve",
            json={"choice": choice},
            headers={"X-Admin-Token": os.environ["TASKFENCE_ADMIN_TOKEN"]})
        body = _json(response)
        payload = {"http": response.status_code,
                   "status": body.get("status"),
                   "executed": body.get("executed"),
                   "detail": body.get("detail")}

    elif action == "session_reads":
        task_id = rest[0]
        from taskfence.persistence import Persistence
        persistence = Persistence()
        payload = {"reads": sorted(
            persistence.lineage.session_asset_ids(task_id))}

    elif action == "decisions":
        task_id = rest[0]
        from taskfence.persistence import Persistence
        rows = Persistence().tasks.decisions(task_id)
        payload = {"decisions": [
            {"decision_id": row["decision_id"], "outcome": row["outcome"],
             "tool": row["tool"], "action": row["action"],
             "created_at": row["created_at"],
             "request_fingerprint": row["request_fingerprint"]}
            for row in rows]}

    elif action == "claim":
        approval_id = rest[0]
        from taskfence.persistence import Persistence
        store = Persistence().approvals
        payload = {"won": store.claim(approval_id, "allowed_once",
                                      resolved_by="child")}

    else:
        print(f"CHILD_JSON: {json.dumps({'error': 'unknown action'})}")
        return 2

    print("CHILD_JSON: " + json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
