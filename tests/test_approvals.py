"""Approval tests (kit Prompts E1, E2): token gate, single-use, deny,
expand_task versioning, and the OpenAPI I3 check."""

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}


@pytest.fixture()
def client(isolate_gateway, monkeypatch):
    # One temporary database for EVERY store (AD-21/AD-25): TASKS included,
    # so no test touches the repository's default SQLite file.
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


def _setup_task(client):
    """Create a task and trigger an APPROVE by sending customer data to the
    (allowed, internal) sales Slack."""
    task_id = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."
    }).json()["task_id"]
    response = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert response["decision"]["outcome"] == "APPROVE", response
    return task_id, response["approval_id"]


def test_agent_side_resolution_without_token_is_403(client):
    _, approval_id = _setup_task(client)
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"})
    assert response.status_code == 403


def test_wrong_token_is_403(client):
    _, approval_id = _setup_task(client)
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"},
                           headers={"X-Admin-Token": "wrong"})
    assert response.status_code == 403


def test_no_token_configured_is_403(client, monkeypatch):
    monkeypatch.delenv("TASKFENCE_ADMIN_TOKEN", raising=False)
    _, approval_id = _setup_task(client)
    response = client.post(f"/approvals/{approval_id}/resolve",
                           json={"choice": "allow_once"}, headers=ADMIN)
    assert response.status_code == 403


def test_allow_once_executes_once_and_only_once(client):
    task_id, approval_id = _setup_task(client)
    first = client.post(f"/approvals/{approval_id}/resolve",
                        json={"choice": "allow_once"}, headers=ADMIN).json()
    assert first["executed"] is True
    assert len(registry.sink_lines("slack_sales")) == 1
    # Identical request afterwards APPROVEs again (approval was single-use):
    second = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert second["decision"]["outcome"] == "APPROVE"
    assert len(registry.sink_lines("slack_sales")) == 1  # unchanged


def test_double_resolve_returns_409(client):
    _, approval_id = _setup_task(client)
    client.post(f"/approvals/{approval_id}/resolve",
                json={"choice": "allow_once"}, headers=ADMIN)
    again = client.post(f"/approvals/{approval_id}/resolve",
                        json={"choice": "allow_once"}, headers=ADMIN)
    assert again.status_code == 409


def test_deny_never_executes(client):
    task_id, approval_id = _setup_task(client)
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "deny"}, headers=ADMIN).json()
    assert result["executed"] is False
    assert len(registry.sink_lines("slack_sales")) == 0


def test_expand_task_creates_new_version_old_unchanged(client):
    task_id, approval_id = _setup_task(client)
    before = client.get(f"/tasks/{task_id}").json()["contract"]
    result = client.post(f"/approvals/{approval_id}/resolve",
                         json={"choice": "expand_task"},
                         headers=ADMIN).json()
    contract = result["contract"]
    assert contract["parent_contract_id"] == before["contract_id"]
    assert "customer_db" in contract["allowed_data"]
    assert contract["allowed_data"] != before["allowed_data"]
    assert contract["version"] != before["version"]
    # Old contract object unchanged in state:
    import taskfence.gateway as gw
    assert gw.TASKS.get_contract(task_id).version == contract["version"]
    # A follow-up with the expanded data no longer violates the source
    # scope; the restricted label still requires human review (kit A-T:
    # a restricted label never yields ALLOW), so it APPROVEs with only
    # the review check remaining - and allow_once executes it.
    followup = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}}).json()
    assert followup["decision"]["outcome"] == "APPROVE"
    assert followup["decision"]["failed_checks"] == \
        ["restricted_label_review"]
    resolved = client.post(f"/approvals/{followup['approval_id']}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    assert resolved["executed"] is True


def test_openapi_has_no_contract_edit_endpoint(client):
    """Invariant I3: no endpoint lets the agent edit a contract."""
    schema = client.get("/openapi.json").json()
    forbidden = ("put", "patch", "delete")
    contractish = [path for path in schema["paths"]
                   if "contract" in path.lower()]
    for path, methods in schema["paths"].items():
        for method in methods:
            assert method.lower() not in forbidden, (method, path)
    assert contractish == [] or all(
        not any(m.lower() in forbidden for m in
                schema["paths"][p]) for p in contractish)


def test_resolution_cannot_bypass_the_pending_state_claim(client):
    """F4: the store exposes no unconditional status setter, and every
    claim is conditional on the record still being pending."""
    import taskfence.gateway as gw
    from taskfence.approvals import ApprovalStore

    assert not hasattr(ApprovalStore, "set_status")

    _, approval_id = _setup_task(client)
    store = gw.APPROVALS
    # An unknown target state is rejected before any write:
    with pytest.raises(ValueError):
        store.claim(approval_id, "pending")
    with pytest.raises(ValueError):
        store.claim(approval_id, "resolved")
    assert store.get(approval_id)["status"] == "pending"

    # The conditional claim is the only transition: first caller wins,
    # every later claim loses no matter which outcome it asks for.
    assert store.claim(approval_id, "allowed_once") is True
    assert store.claim(approval_id, "denied") is False
    assert store.claim(approval_id, "expanded") is False
    assert store.get(approval_id)["status"] == "allowed_once"
    # The gateway honours the consumed record too:
    assert client.post(f"/approvals/{approval_id}/resolve",
                       json={"choice": "deny"},
                       headers=ADMIN).status_code == 409


def test_unknown_approval_404(client):
    response = client.post("/approvals/apr-nope/resolve",
                           json={"choice": "deny"}, headers=ADMIN)
    assert response.status_code == 404
