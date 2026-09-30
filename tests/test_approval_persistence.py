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

These tests simulate a gateway restart by deleting the task's in-memory
state (CONTRACTS/TASK_TEXT/LAST_DECISION) while keeping the SQLite-backed
stores — exactly what a real restart does to process memory while
preserving the database.
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
def client(tmp_path, monkeypatch):
    """Real gateway app with every store bound to tmp_path SQLite files
    and the outbox isolated — the same layout a restarted gateway would
    re-open from disk."""
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    import taskfence.gateway as gw
    from taskfence.audit import AuditLog, TaskStateStore
    from taskfence.approvals import ApprovalStore
    from taskfence.lineage import LineageTracker
    monkeypatch.setattr(gw, "TRACKER",
                        LineageTracker(db_path=tmp_path / "lin.db"))
    monkeypatch.setattr(gw, "AUDIT", AuditLog(tmp_path / "audit.db"))
    monkeypatch.setattr(gw, "APPROVALS", ApprovalStore(tmp_path / "appr.db"))
    monkeypatch.setattr(gw, "STATE", TaskStateStore(tmp_path / "state.db"))
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client, gw
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


def _simulate_restart(gw, task_id):
    """Recreate what a gateway restart does to PROCESS memory (SQLite
    stores on disk are untouched): in-memory maps lose the task."""
    gw.CONTRACTS.pop(task_id, None)
    gw.TASK_TEXT.pop(task_id, None)
    gw.LAST_DECISION.pop(task_id, None)


def _resolve(client, approval_id, choice="allow_once"):
    return client.post(f"/approvals/{approval_id}/resolve",
                       json={"choice": choice}, headers=ADMIN)


def _sink_lines():
    return registry.sink_lines("slack_sales")


# --- TEST 1: persisted approval resolves after the task leaves memory -------


def test_persisted_approval_resolves_after_restart(client):
    client, gw = client
    task_id, approval_id, args = _approve(client)
    assert approval_id in {row["approval_id"]
                           for row in gw.APPROVALS.pending()}

    _simulate_restart(gw, task_id)
    assert task_id not in gw.CONTRACTS      # the exact 404 trigger, removed
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
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
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
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    gw.APPROVALS.conn.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps({"request": {}}), approval_id))
    gw.APPROVALS.conn.commit()

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
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    record = gw.APPROVALS.get(approval_id)
    record["contract_snapshot"]["purpose"] = "hr_analytics"  # tampered
    record["contract_id"] = "c-forged"
    gw.APPROVALS.conn.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(record), approval_id))
    gw.APPROVALS.conn.commit()

    response = _resolve(client, approval_id)
    assert response.status_code == 409
    assert _sink_lines() == []
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


def test_missing_exact_tool_args_fails_closed_after_restart(client):
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    record = gw.APPROVALS.get(approval_id)
    record["tool_args"] = None   # FIX1: no exact args -> no execution
    gw.APPROVALS.conn.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(record), approval_id))
    gw.APPROVALS.conn.commit()

    response = _resolve(client, approval_id)
    assert response.status_code == 409
    assert "exact original tool args" in response.text
    assert _sink_lines() == []
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


# --- TEST 3: single-use, restart does not resurrect a consumed approval -----


def test_second_resolution_is_rejected_409(client):
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    first = _resolve(client, approval_id)
    assert first.status_code == 200
    assert len(_sink_lines()) == 1

    second = _resolve(client, approval_id)
    assert second.status_code == 409
    assert "already resolved" in second.text
    assert len(_sink_lines()) == 1          # no second execution/delivery


def test_resolved_before_restart_stays_resolved_after(client):
    """A restart must not resurrect an already-consumed approval."""
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    assert _resolve(client, approval_id).status_code == 200
    delivered = _sink_lines()[0]["text"]
    _simulate_restart(gw, task_id)
    again = _resolve(client, approval_id)
    assert again.status_code == 409
    assert [row["text"] for row in _sink_lines()] == [delivered]


# --- TEST 4: exact payload preservation across the restart boundary ---------


def test_exact_payload_preserved_across_restart(client):
    client, gw = client
    task_id, approval_id, args = _approve(client)
    _simulate_restart(gw, task_id)
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
    """expand_task installs a NEW live contract version — it requires the
    task's live session, so after a restart it fails closed (409) and the
    record stays pending; deny/allow_once remain available."""
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 409
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    # The same approval still resolves by allow_once (or deny):
    assert _resolve(client, approval_id).status_code == 200


def test_expand_task_works_while_task_is_live(client):
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    response = _resolve(client, approval_id, choice="expand_task")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "expanded"
    assert body["contract"]["parent_contract_id"]
    assert gw.CONTRACTS[task_id].parent_contract_id


# --- admin gate + sink discipline -------------------------------------------


def test_resolution_requires_admin_token_after_restart(client):
    client, gw = client
    task_id, approval_id, _ = _approve(client)
    _simulate_restart(gw, task_id)
    no_token = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"})
    assert no_token.status_code == 403
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"
    assert _sink_lines() == []
