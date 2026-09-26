"""Approval end-to-end tests (kit Prompt E-T): audit ordering
request -> approval -> execution, both contract versions in the trail,
re-approve cycle, deny leaves sinks untouched."""

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    import taskfence.gateway as gw
    from taskfence.audit import AuditLog
    from taskfence.lineage import LineageTracker
    from taskfence.approvals import ApprovalStore
    monkeypatch.setattr(gw, "TRACKER",
                        LineageTracker(db_path=tmp_path / "lin.db"))
    monkeypatch.setattr(gw, "AUDIT", AuditLog(tmp_path / "audit.db"))
    monkeypatch.setattr(gw, "APPROVALS", ApprovalStore(tmp_path / "appr.db"))
    from taskfence.audit import TaskStateStore
    monkeypatch.setattr(gw, "STATE", TaskStateStore(tmp_path / "state.db"))
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


def _request_approval(client):
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    call = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert call["decision"]["outcome"] == "APPROVE"
    return task_id, call["approval_id"]


def test_audit_order_request_then_approval_then_execution(client):
    task_id, approval_id = _request_approval(client)
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    events = client.get("/audit").json()["events"]
    assert [e["action"] for e in events] == ["send_message",
                                             "approval_resolve"]
    assert events[0]["decision"] == "APPROVE"
    assert events[1]["decision"] == "ALLOW"
    assert "Human approved" in events[1]["reasons"]


def test_expanded_task_shows_both_versions_in_audit(client):
    task_id, approval_id = _request_approval(client)
    old_version = client.get(f"/tasks/{task_id}").json()["contract"]["version"]
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "expand_task"},
                         headers=ADMIN).json()
    new_version = result["contract"]["version"]
    # Follow-up under the new version: restricted label still needs human
    # review -> APPROVE -> allow_once executes it (kit scenario D story).
    followup = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert followup["decision"]["failed_checks"] == \
        ["restricted_label_review"]
    client.post(f"/approvals/{followup['approval_id']}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    events = client.get("/audit").json()["events"]
    versions = [e["contract_version"] for e in events]
    assert old_version in versions and new_version in versions
    assert len(set(versions)) == 2
    # The expansion event names the parent as unchanged (reasons are stored
    # as a joined string):
    expand_event = next(e for e in events
                        if e["transformation"] == "expand_task")
    assert "parent" in expand_event["reasons"]
    assert len(registry.sink_lines("slack_sales")) == 1


def test_allow_once_cycle_reapprove_flow(client):
    task_id, approval_id = _request_approval(client)
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    assert len(registry.sink_lines("slack_sales")) == 1
    # Identical request again -> APPROVE again -> new approval id:
    second = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert second["decision"]["outcome"] == "APPROVE"
    assert second["approval_id"] and second["approval_id"] != approval_id
    # Deny this time: sinks stay at one line.
    client.post(f"/approvals/{second['approval_id']}/resolve",
                json={"choice": "deny"}, headers=ADMIN)
    assert len(registry.sink_lines("slack_sales")) == 1


def test_deny_leaves_sinks_untouched_and_audited(client):
    _, approval_id = _request_approval(client)
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "deny"}, headers=ADMIN).json()
    assert result["executed"] is False
    assert len(registry.sink_lines("slack_sales")) == 0
    events = client.get("/audit").json()["events"]
    deny_events = [e for e in events if e["action"] == "approval_resolve"]
    assert len(deny_events) == 1 and deny_events[0]["decision"] == "BLOCK"


def test_full_demo_story_conversion_rate_case(client):
    """The Gate E narrative: agent needs customer data for regional
    conversion rates -> APPROVE -> allow_once -> succeeds."""
    task_id, approval_id = _request_approval(client)
    resolved = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    assert resolved["executed"] is True
    assert resolved["result"]["delivered"] is True
    lines = registry.sink_lines("slack_sales")
    assert len(lines) == 1 and "regional conversion" in lines[0]["text"]
