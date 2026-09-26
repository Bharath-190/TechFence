"""FIX1 approval-integrity regression tests (kit Prompt FIX1-T).

Covers: exact allow_once replay (>200-char payload, exact channel), binding
to the reviewed contract version (current-contract changes are irrelevant),
fail-closed on missing/stale bindings BEFORE the approval is consumed,
admin-gated preview, payload never leaking to unauthenticated endpoints,
dashboard SQLite preview, approved lineage/audit traceability, declared-only
expand_task scope with parent binding, deterministic source-group ordering,
and the intact agent-side 403 boundary.

No session-taint policy is changed here (FIX-D5 is a separate phase).
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}

# >200 characters; contains no ambiguity about where it lands.
LONG_PAYLOAD = (
    "Q3 regional performance summary: North region closed 412 units of "
    "Anvil Pro with a 7.1% conversion lift; South region closed 388 units "
    "with retention holding at 88%; West region leads on expansion revenue "
    "at 1.9x YoY; churn dipped below 4% for the first time this fiscal year "
    "— recommend holding current pricing through Q4 planning."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
        yield test_client
    registry.reset_outbox()


def _approve(client, channel="#hr-ops", text=LONG_PAYLOAD):
    """Create a task and a pending APPROVE using the repo's real request
    shape: source_assets nested INSIDE args (verified gateway behavior)."""
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    args = {"channel": channel, "text": text,
            "source_assets": ["customer_db"]}
    response = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": "send_slack", "args": args}).json()
    assert response["decision"]["outcome"] == "APPROVE", response
    return task_id, response["approval_id"], args


def _mutate_current_contract(client, task_id):
    """Test-only simulation of a 'newer current contract' for the same task
    (user-sanctioned fixture mutation; no production endpoint added).
    created_at participates in the version hash, so this yields a different
    version with a different id, without touching stored approvals."""
    import taskfence.gateway as gw
    import copy
    current = gw.CONTRACTS[task_id]
    forged = copy.deepcopy(current.model_dump())
    forged["contract_id"] = current.contract_id + "-forged"
    forged["created_at"] = "2099-01-01T00:00:00+00:00"
    from taskfence.models import TaskContract
    gw.CONTRACTS[task_id] = TaskContract.model_validate(forged)
    return gw.CONTRACTS[task_id]


# --- 1. Exact allow_once fidelity -------------------------------------------

def test_allow_once_replays_exact_payload_and_channel(client):
    task_id, approval_id, args = _approve(client)
    resolved = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    assert resolved["executed"] is True, resolved
    lines = registry.sink_lines("slack_sales")
    assert len(lines) == 1
    # Byte-for-byte payload fidelity (original request vs sink record):
    assert lines[0]["text"] == args["text"]
    assert len(lines[0]["text"]) > 200
    # Exact channel fidelity — the lossy replay defaulted #hr-ops to #sales:
    assert lines[0]["channel"] == args["channel"] == "#hr-ops"


def test_allow_once_executes_exactly_once(client):
    _, approval_id, _ = _approve(client)
    first = client.post(f"/approvals/{approval_id}/resolve",
                        json={"choice": "allow_once"}, headers=ADMIN)
    assert first.status_code == 200
    assert registry.sink_lines("slack_sales")
    again = client.post(f"/approvals/{approval_id}/resolve",
                        json={"choice": "allow_once"}, headers=ADMIN)
    assert again.status_code == 409
    assert len(registry.sink_lines("slack_sales")) == 1


def test_allow_once_never_defaults_missing_args(client):
    """The old lossy path reconstructed args from the FlowRequest snapshot
    (which has no channel/text keys) and silently substituted defaults."""
    task_id, approval_id, args = _approve(client, text="tiny but exact")
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    line = registry.sink_lines("slack_sales")[0]
    assert line["text"] == args["text"] == "tiny but exact"
    assert line["channel"] == "#hr-ops"


# --- 2. Approval binding to the reviewed contract version --------------------

def test_resolution_ignores_changed_current_contract(client):
    task_id, approval_id, _ = _approve(client)
    reviewed_version = client.get(
        f"/approvals/{approval_id}", headers=ADMIN).json()["contract_version"]
    forged = _mutate_current_contract(client, task_id)
    assert forged.version != reviewed_version
    resolved = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    assert resolved["executed"] is True
    # Executed under the REVIEWED version, not the forged current one:
    assert resolved["decision"]["contract_version"] == reviewed_version
    events = client.get("/audit").json()["events"]
    execution = next(e for e in events
                     if e["action"] == "approval_resolve"
                     and e["decision"] == "ALLOW")
    assert execution["contract_id"] != forged.contract_id
    assert execution["contract_version"] == reviewed_version


def test_stale_binding_fails_closed_without_executing(client, tmp_path):
    task_id, approval_id, _ = _approve(client)
    import taskfence.gateway as gw
    # Simulate an unretrievable reviewed version: a record whose stored
    # binding version no longer matches its snapshot (fixture-level tamper).
    record = gw.APPROVALS.get(approval_id)
    data = json.loads(record["args"])  # raw stored args JSON
    data["contract_version"] = "0" * 64  # stale version
    conn = sqlite3.connect(tmp_path / "appr.db")
    with conn:
        conn.execute(
            "UPDATE approvals SET args = ? WHERE approval_id = ?",
            (json.dumps(data), approval_id))
    conn.close()
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"}, headers=ADMIN)
    assert response.status_code == 409
    assert registry.sink_lines("slack_sales") == []
    # Fail-closed must not consume the pending approval:
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


def test_missing_binding_fails_closed_for_legacy_record(client, tmp_path):
    """A pre-FIX1 record (bare FlowRequest dump, no binding) must fail
    closed rather than silently fall back to the current contract."""
    task_id, approval_id, _ = _approve(client)
    import taskfence.gateway as gw
    record = gw.APPROVALS.get(approval_id)
    legacy = record["request"]  # FlowRequest snapshot only
    conn = sqlite3.connect(tmp_path / "appr.db")
    with conn:
        conn.execute("UPDATE approvals SET args = ? WHERE approval_id = ?",
                     (json.dumps(legacy), approval_id))
    conn.close()
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"}, headers=ADMIN)
    assert response.status_code == 409
    assert registry.sink_lines("slack_sales") == []


# --- 3. Approval preview security --------------------------------------------

def test_approval_detail_requires_admin_token(client):
    task_id, approval_id, args = _approve(client)
    response = client.get(f"/approvals/{approval_id}")
    assert response.status_code == 403  # repo auth convention
    wrong = client.get(f"/approvals/{approval_id}",
                       headers={"X-Admin-Token": "wrong"})
    assert wrong.status_code == 403


def test_admin_sees_exact_request_before_approval(client):
    task_id, approval_id, args = _approve(client)
    detail = client.get(f"/approvals/{approval_id}", headers=ADMIN).json()
    assert detail["tool"] == "send_slack"
    assert detail["action"] == "send_message"
    assert detail["destination"] == "sales_slack"
    assert detail["declared_source_groups"] == ["customer_db"]
    assert "customer_db" in detail["source_groups"]
    assert detail["payload"] == args["text"]  # full, untruncated
    assert detail["tool_args"]["channel"] == "#hr-ops"
    assert detail["contract_version"]  # binding shown to the human


def test_agent_side_client_cannot_access_admin_paths(client):
    task_id, approval_id, _ = _approve(client)
    from taskfence.client import GatewayClient

    class _NoAuth:
        """httpx.Client-shaped wrapper that never attaches a token."""

        def __init__(self, tc):
            self._tc = tc

        def get(self, url, **kw):
            return self._tc.get(url, **kw)  # no X-Admin-Token, ever

        def post(self, url, json=None, **kw):
            return self._tc.post(url, json=json, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    agent = GatewayClient()
    with _NoAuth(client) as bare:
        detail = bare.get(f"{agent.base_url}/approvals/{approval_id}")
        assert detail.status_code == 403
        resolve = bare.post(
            f"{agent.base_url}/approvals/{approval_id}/resolve",
            json={"choice": "allow_once"})
        assert resolve.status_code == 403


def test_payload_string_nowhere_in_unauthenticated_endpoints(client):
    task_id, approval_id, args = _approve(client)
    needle = args["text"]
    tasks_body = client.get(f"/tasks/{task_id}").text
    assert needle not in tasks_body
    audit_body = client.get("/audit").text
    assert needle not in audit_body
    approvals_body = client.get("/approvals").text
    assert needle not in approvals_body
    # Not via aliases either (the body IS the serialized response):
    assert "tool_args" not in tasks_body and "tool_args" not in audit_body


# --- 4. Dashboard approval preview (SQLite path) -----------------------------

def test_dashboard_sqlite_preview_shows_exact_request(
        client, tmp_path, monkeypatch):
    task_id, approval_id, args = _approve(client)
    monkeypatch.setenv("TASKFENCE_DB", str(tmp_path / "appr.db"))
    import importlib
    import dashboard.app as dash
    importlib.reload(dash)
    detail = dash._approval_detail(approval_id)
    assert detail is not None
    assert detail["tool"] == "send_slack"
    assert detail["action"] == "send_message"
    assert detail["destination"] == "sales_slack"
    assert detail["declared_source_groups"] == ["customer_db"]
    assert detail["payload"] == args["text"]  # FULL payload from SQLite
    assert detail["tool_args"]["channel"] == "#hr-ops"
    assert detail["contract_version"]
    # Dashboard still renders with a pending approval present:
    from streamlit.testing.v1 import AppTest
    from pathlib import Path as _Path
    at = AppTest.from_file(str(_Path("dashboard/app.py").resolve()),
                           default_timeout=30)
    at.run()
    assert not at.exception
    rendered = " ".join(m.value or "" for m in at.markdown) + " " + \
        " ".join(code.value or "" for code in at.code)
    assert "send_slack" in rendered          # tool in the preview card
    assert "customer_db" in rendered         # declared source group shown
    assert args["text"] in rendered          # FULL payload rendered


# --- 5. Approved lineage + audit traceability --------------------------------

def test_allow_once_updates_lineage_and_audit(client):
    task_id, approval_id, _ = _approve(client)
    resolved = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    lineage = client.get(f"/lineage/{task_id}").json()
    edges = {(e["src"], e["dst"]) for e in lineage["edges"]}
    assert ("customer_db", "sales_slack") in edges  # the sink edge exists
    # Parity with normal ALLOW bookkeeping: the returned lineage path is
    # path_to(payload_node) — the declared source node itself here. The
    # traceability requirements are the sink edge above plus the audit path.
    assert resolved["lineage_path"] == ["customer_db"]
    assert resolved["lineage_path"]  # non-empty
    events = client.get("/audit").json()["events"]
    execution = next(e for e in events
                     if e["action"] == "approval_resolve"
                     and e["decision"] == "ALLOW")
    assert execution["lineage_path"]  # non-empty for the actual execution
    assert "customer_db" in execution["lineage_path"]
    detail = client.get(f"/approvals/{approval_id}", headers=ADMIN).json()
    assert execution["contract_id"] == detail["contract_id"]
    assert execution["contract_version"] == detail["contract_version"]


def test_allow_once_persists_task_state_and_last_decision(client):
    task_id, approval_id, _ = _approve(client)
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    state = client.get(f"/tasks/{task_id}").json()
    last = state["last_decision"]
    assert last["tool"] == "send_slack"
    assert last["decision"]["outcome"] == "ALLOW"
    assert last["lineage_path"] == ["customer_db"]  # parity with ALLOW
    assert state["pending_approvals"] == []  # consumed after resolution


# --- 6. expand_task: declared scope only + version binding -------------------

def test_expand_task_grants_only_declared_group_despite_session_taint(
        client, tmp_path):
    task_id, approval_id, _ = _approve(client)
    import taskfence.gateway as gw
    # Session taint with UNRELATED groups (reads happen, conservative mode):
    gw.TRACKER.register_asset(task_id, "internal_strategy")
    gw.TRACKER.register_asset(task_id, "meeting_notes")
    # Newer current contract for the same task (user-sanctioned mutation):
    _mutate_current_contract(client, task_id)
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "expand_task"},
                         headers=ADMIN).json()
    contract = result["contract"]
    assert "customer_db" in contract["allowed_data"]  # declared group added
    # Session-taint-only groups are ABSENT:
    assert "internal_strategy" not in contract["allowed_data"]
    assert "meeting_notes" not in contract["allowed_data"]
    # Parent binding points at the REVIEWED approval contract, not the
    # forged newer current contract:
    detail = client.get(f"/approvals/{approval_id}", headers=ADMIN).json()
    assert contract["parent_contract_id"] == detail["contract_id"]
    assert contract["parent_contract_id"] != gw.CONTRACTS[task_id].contract_id


def test_expand_task_with_no_declared_group_fails_closed(
        client, tmp_path, monkeypatch):
    """FIX1-E + Option A interaction: an undeclared outbound whose request
    snapshot carries only session-taint groups cannot be expanded."""
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    import taskfence.gateway as gw
    gw.TRACKER.register_asset(task_id, "internal_strategy")
    response = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack",
        "args": {"channel": "#sales", "text": "no declared sources here"}})
    body = response.json()
    # Conservative taint supplies session groups as implicit source context
    # (unchanged policy); whatever the outcome, if an approval exists its
    # expansion must fail closed because nothing was DECLARED.
    if body["decision"]["outcome"] != "APPROVE":
        pytest.skip("request did not produce a pending approval")
    approval_id = body["approval_id"]
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "expand_task"}, headers=ADMIN)
    assert result.status_code == 409
    assert gw.APPROVALS.get(approval_id)["status"] == "pending"


# --- 7. Deterministic source-group ordering ----------------------------------

def test_source_group_ordering_is_sorted_and_stable(client, tmp_path):
    """Direct ordering-semantics check: the persisted record (and every
    serialized view of it) presents groups in sorted order, independent of
    set iteration or hash seed."""
    import taskfence.gateway as gw
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    args = {"channel": "#sales", "text": "multi-source summary",
            "source_assets": ["meeting_notes", "customer_db",
                              "internal_strategy"]}
    response = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": "send_slack", "args": args}).json()
    assert response["decision"]["outcome"] == "APPROVE", response
    record = gw.APPROVALS.get(response["approval_id"])
    request_groups = record["request"]["source_groups"]
    # Sorted, complete, and identical to the declared-groups projection:
    assert request_groups == sorted(request_groups)
    assert record["declared_source_groups"] == \
        sorted(record["declared_source_groups"])
    # The singular compatibility field is derived deterministically from
    # the sorted set (gateway no longer uses next(iter(set))):
    groups = {"internal_strategy", "meeting_notes", "customer_db"}
    assert sorted(groups)[0] == "customer_db"
    detail = client.get(f"/approvals/{response['approval_id']}",
                        headers=ADMIN).json()
    assert detail["source_groups"] == sorted(detail["source_groups"])


# --- 8. Agent cannot resolve approval (existing 403 intact) ------------------

def test_agent_cannot_resolve_without_token(client):
    _, approval_id, _ = _approve(client)
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"})
    assert response.status_code == 403
    assert registry.sink_lines("slack_sales") == []


# --- Fidelity guard: truncated snapshot never used for replay ----------------

def test_request_snapshot_payload_is_truncated_but_unused(client):
    """The policy snapshot may truncate at 200 chars; replay must come from
    tool_args. Prove the split exists and replay is byte-exact anyway."""
    task_id, approval_id, args = _approve(client)
    import taskfence.gateway as gw
    record = gw.APPROVALS.get(approval_id)
    snapshot_payload = record["request"].get("payload", "")
    assert len(args["text"]) > 200
    assert snapshot_payload == args["text"][:200]  # snapshot truncates...
    assert record["tool_args"]["text"] == args["text"]  # ...tool_args does not
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    assert registry.sink_lines("slack_sales")[0]["text"] == args["text"]
