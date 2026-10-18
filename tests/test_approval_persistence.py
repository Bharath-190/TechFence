"""Restart-safe approval resolution regression tests (persistence bug fix).

THE BUG: resolve_approval() gated every choice on `task_id in CONTRACTS`,
an in-memory dict wiped by a gateway restart, while the approval record
itself (SQLite) survives with its complete frozen FIX1 binding. A
legitimately persisted approval was therefore impossible to resolve
(HTTP 404 "unknown task") exactly when persistence mattered.

THE FIX: deny and allow_once resolve from the approval record's OWN frozen
binding (contract snapshot + id/version, EXACT original tool args, stored
FlowRequest snapshot, declared sources) — never from the live CONTRACTS
map, never from a rebuilt contract, never from reconstructed task text.
All FIX1 fail-closed checks run BEFORE the approval is claimed or anything
executes. expand_task still belongs to the live session and fails closed
when the task is not active in this gateway process.

Phase 2 moved authoritative task, contract and decision state into the
database, so these tests simulate a gateway restart by rebuilding every
store from the same on-disk database (fresh `Database` objects, no shared
process memory) — exactly what a restarted process sees.
"""

import json

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}

DISTINCTIVE_PAYLOAD = (
    "PERSISTENCE-CHECK-9f3a: North region closed 412 units of Anvil Pro; "
    "conversion lift 7.1%; churn below 4% — unique marker 0xE3B0C442."
)


@pytest.fixture()
def client(isolate_gateway, monkeypatch):
    """Real gateway app bound to ONE temp database plus a `restart()`
    helper that rebuilds every store from that same database — what a
    real restart of the process leaves in place."""
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    import taskfence.gateway as gw
    from taskfence.approvals import ApprovalStore
    from taskfence.audit import AuditLog, TaskStateStore
    from taskfence.config import Config
    from taskfence.lineage import LineageTracker
    from taskfence.persistence import Database, TaskStore

    def restart():
        db = Database(Config.from_env(
            {"TASKFENCE_DB": str(isolate_gateway.sqlite_path)}))
        monkeypatch.setattr(gw, "TASKS", TaskStore(db))
        monkeypatch.setattr(gw, "TRACKER", LineageTracker(db))
        monkeypatch.setattr(gw, "AUDIT", AuditLog(db))
        monkeypatch.setattr(gw, "APPROVALS", ApprovalStore(db))
        monkeypatch.setattr(gw, "STATE", TaskStateStore(db))
        # F2: a restart also loses process-local review sessions. Durable
        # state survives; expansion liveness does not.
        monkeypatch.setattr(gw, "LIVE_TASKS", gw.LIVE_TASKS.__class__())
        return db

    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client, gw, restart
    registry.reset_outbox()


def _approve(client, text=DISTINCTIVE_PAYLOAD, channel="#hr-ops"):
    """Create a task + pending approval via the real gateway flow."""
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    args = {"channel": channel, "text": text,
            "source_assets": ["customer_db"]}
    response = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": "send_slack", "args": args}).json()
    assert response["decision"]["outcome"] == "APPROVE", response
    return task_id, response["approval_id"], args


def _resolve(client, approval_id, choice="allow_once"):
    return client.post(f"/approvals/{approval_id}/resolve",
                       json={"choice": choice}, headers=ADMIN)


def _sink_lines():
    return registry.sink_lines("slack_sales")


# --- TEST 1: persisted approval resolves after the task leaves memory -------


def test_persisted_approval_resolves_after_restart(client):
    client, gw, restart = client
    task_id, approval_id, args = _approve(client)
    assert approval_id in {row["approval_id"]
                           for row in gw.APPROVALS.pending()}

    restart()
    # Durable authority: the restarted process still resolves the task
    # from the database (no in-memory map involved).
    assert gw.TASKS.get_contract(task_id) is not None
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"

    response = _resolve(client, approval_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "allowed_once"
    assert body["executed"] is True
    assert body["decision"]["outcome"] == "ALLOW"

    # The EXACT original tool args were replayed (payload verification
    # also covers TEST 4):
    assert len(_sink_lines()) == 1          # exactly one execution
    delivered = _sink_lines()[0]
    assert delivered["text"] == args["text"]
    assert delivered["channel"] == args["channel"]
    assert delivered["task_id"] == task_id

    # Audit contains the approval_resolve ALLOW event with lineage:
    events = client.get(f"/audit?task_id={task_id}").json()["events"]
    resolves = [e for e in events if e["action"] == "approval_resolve"]
    assert len(resolves) == 1
    assert resolves[0]["decision"] == "ALLOW"
    assert resolves[0]["lineage_path"]      # lineage recorded
    assert "customer_db" in resolves[0]["lineage_path"]

    # Approval consumed exactly once:
    assert gw.APPROVALS.get(approval_id)["status"] == "allowed_once"
    assert gw.APPROVALS.pending() == []


def test_denied_persisted_approval_resolves_after_restart(client):
    """Deny must work for persisted approvals too — the human can always
    reject, restart or not (executes nothing)."""
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    response = _resolve(client, approval_id, choice="deny")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "denied"
    assert body["executed"] is False
    assert _sink_lines() == []
    events = client.get(f"/audit?task_id={task_id}").json()["events"]
    resolves = [e for e in events if e["action"] == "approval_resolve"]
    assert len(resolves) == 1 and resolves[0]["decision"] == "BLOCK"


# --- TEST 2: stale/missing binding still fails closed -----------------------


def test_missing_contract_binding_fails_closed_after_restart(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps({"request": {}}), approval_id))

    response = _resolve(client, approval_id)
    assert response.status_code == 409
    assert "fail closed" in response.text
    # Nothing executed, approval stays pending for a re-decision:
    assert _sink_lines() == []
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    resolves = [e for e in client.get(
        f"/audit?task_id={task_id}").json()["events"]
        if e["action"] == "approval_resolve"]
    assert resolves == []


def test_corrupted_contract_snapshot_fails_closed_after_restart(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    record = gw.APPROVALS.get(approval_id)
    record["contract_snapshot"]["purpose"] = "hr_analytics"  # tampered
    record["contract_id"] = "c-forged"
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(record), approval_id))

    response = _resolve(client, approval_id)
    assert response.status_code == 409
    assert _sink_lines() == []
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


def test_missing_exact_tool_args_fails_closed_after_restart(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    record = gw.APPROVALS.get(approval_id)
    record["tool_args"] = None   # FIX1: no exact args -> no execution
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(record), approval_id))

    response = _resolve(client, approval_id)
    assert response.status_code == 409
    assert "exact original tool args" in response.text
    assert _sink_lines() == []
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


# --- TEST 3: single-use, restart does not resurrect a consumed approval -----


def test_second_resolution_is_rejected_409(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    first = _resolve(client, approval_id)
    assert first.status_code == 200
    assert len(_sink_lines()) == 1

    second = _resolve(client, approval_id)
    assert second.status_code == 409
    assert "already resolved" in second.text
    assert len(_sink_lines()) == 1          # no second execution/delivery


def test_resolved_before_restart_stays_resolved_after(client):
    """A restart must not resurrect an already-consumed approval."""
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    assert _resolve(client, approval_id).status_code == 200
    delivered = _sink_lines()[0]["text"]
    restart()
    again = _resolve(client, approval_id)
    assert again.status_code == 409
    assert [row["text"] for row in _sink_lines()] == [delivered]


# --- TEST 4: exact payload preservation across the restart boundary ---------


def test_exact_payload_preserved_across_restart(client):
    client, gw, restart = client
    task_id, approval_id, args = _approve(client)
    restart()
    resolved = _resolve(client, approval_id)
    assert resolved.status_code == 200
    # The tool result confirms the executed channel; the EXACT payload is
    # verified value-for-value from the sink record below.
    assert resolved.json()["result"]["channel"] == args["channel"]
    delivered = _sink_lines()[0]
    assert delivered["text"] == args["text"]          # value-for-value
    assert delivered["channel"] == args["channel"]
    # Outbound sink records carry an empty asset_id by long-standing
    # convention (same as any normal ALLOW execution); the DECLARED source
    # is preserved in the stored audit row instead:
    events = client.get(f"/audit?task_id={task_id}").json()["events"]
    resolves = [e for e in events if e["action"] == "approval_resolve"]
    assert "customer_db" in resolves[0]["sources"].split(",")


# --- expand_task: live-session semantics unchanged --------------------------


def test_expand_task_after_restart_fails_closed_pending(client):
    """F2 (blocking): `expand_task` issues a NEW contract version for a live
    review session. After a restart there is no live session, so the pending
    record is preserved, nothing expands and nothing dispatches — deny /
    allow_once remain available. A stale expansion approval can never widen
    scope indefinitely."""
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    version_before = gw.TASKS.get_contract(task_id).version
    restart()
    # The task itself is durable and visible to the restarted process:
    assert gw.TASKS.get_contract(task_id) is not None
    assert gw.TASKS.get_contract(task_id).version == version_before

    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 409
    assert "live review session" in response.json()["detail"]
    # Not consumed, not expanded, not dispatched:
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    assert gw.TASKS.get_contract(task_id).version == version_before
    assert _sink_lines() == []
    # Repeated attempts stay fail-closed and still do not consume it:
    assert _resolve(client, approval_id,
                    choice="expand_task").status_code == 409
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    assert _sink_lines() == []
    # The human can still resolve it safely, exactly once (replay is a 409):
    assert _resolve(client, approval_id).status_code == 200
    assert len(_sink_lines()) == 1
    assert _resolve(client, approval_id, choice="expand_task").status_code == 409
    assert len(_sink_lines()) == 1


def test_expand_task_with_stale_version_binding_fails_closed(client):
    """A live session is not sufficient: the record must still match the
    contract version the human reviewed (FIX1-E + F2)."""
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    record = gw.APPROVALS.get(approval_id)
    data = json.loads(record["args"])
    data["contract_version"] = "0" * 64          # stale reviewed version
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(data), approval_id))
    version_before = gw.TASKS.get_contract(task_id).version

    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 409
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    assert gw.TASKS.get_contract(task_id).version == version_before
    assert _sink_lines() == []


def test_expand_task_fails_closed_when_task_is_gone(client):
    """When the durable task row is absent there is no reviewed live
    contract to expand: 409, fail closed, approval stays pending."""
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    gw.TASKS.db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 409
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    # The same approval still resolves by allow_once (nothing was consumed):
    assert _resolve(client, approval_id).status_code == 200


def test_expand_task_works_while_task_is_live(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "expanded"
    assert body["contract"]["parent_contract_id"]
    assert gw.TASKS.get_contract(task_id).parent_contract_id


# --- admin gate + sink discipline -------------------------------------------


def test_resolution_requires_admin_token_after_restart(client):
    client, gw, restart = client
    task_id, approval_id, _ = _approve(client)
    restart()
    no_token = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"})
    assert no_token.status_code == 403
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    assert _sink_lines() == []
