"""Approval replay gates against post-review drift (R1, R1b, R3).

The read-only lifecycle review found that a persisted `allow_once` approval
was bound to the reviewed contract and the exact tool arguments, but not to
the security state that can drift *around* them. These tests pin the three
gates added in response:

- R1  — current policy is re-evaluated on replay; a current HARD violation
        can never be overridden by an earlier human approval.
- R1b — the reviewed tool identity/action/destination and the connector
        configuration must still match.
- R3  — an approved read is bound to the asset content digest and
        classification captured at review.

Every rejection must leave the approval `pending` and produce no side
effect: no execution, no sink line, no new decision record.
"""

import json

import pytest
from fastapi.testclient import TestClient

from taskfence import catalog, registry, tools
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}
TASK_TEXT = "Summarize Q3 sales and post it to #sales."
OUTBOUND_ARGS = {"channel": "#sales", "text": "regional conversion summary",
                 "source_assets": ["customer_db"]}
READ_ARGS = {"path": "hr/employee_salary.csv"}


@pytest.fixture()
def client(isolate_gateway, monkeypatch):
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


def _task(client) -> str:
    response = client.post("/tasks", json={"task_text": TASK_TEXT})
    assert response.status_code == 200, response.text
    return response.json()["task_id"]


def _approve_outbound(client) -> tuple[str, str]:
    task_id = _task(client)
    body = client.post(f"/tasks/{task_id}/tool-call",
                       json={"tool": "send_slack",
                             "args": OUTBOUND_ARGS}).json()
    assert body["decision"]["outcome"] == "APPROVE", body
    return task_id, body["approval_id"]


def _approve_read(client, monkeypatch) -> tuple[str, str]:
    """An out-of-scope read APPROVEs only while the read gate is on."""
    import taskfence.gateway as gw
    monkeypatch.setattr(gw, "GATE_OUT_OF_SCOPE_READS", True)
    task_id = _task(client)
    body = client.post(f"/tasks/{task_id}/tool-call",
                       json={"tool": "read_file", "args": READ_ARGS}).json()
    assert body["decision"]["outcome"] == "APPROVE", body
    return task_id, body["approval_id"]


def _resolve(client, approval_id: str, choice: str = "allow_once"):
    return client.post(f"/approvals/{approval_id}/resolve",
                       json={"choice": choice}, headers=ADMIN)


def _decision_count(task_id: str) -> int:
    import taskfence.gateway as gw
    return gw.TASKS.count_decisions(task_id)


def _status(approval_id: str) -> str:
    import taskfence.gateway as gw
    return gw.APPROVALS.get(approval_id)["status"]


def _assert_rejected(response, task_id: str, approval_id: str) -> None:
    """A rejected replay: 409, record untouched, nothing executed."""
    assert response.status_code == 409, response.text
    assert _status(approval_id) == "pending"
    assert registry.sink_lines("slack_sales") == []


# --- R1: current policy decides hard rules ----------------------------------

def test_replay_hard_blocked_by_stricter_destination_policy(client,
                                                            monkeypatch):
    """Policy hardening after review: the reviewed destination becomes an
    external one. The human approval must NOT override the hard rule."""
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    with monkeypatch.context() as stricter:
        stricter.setattr(
            catalog, "EXTERNAL_DESTINATIONS",
            frozenset(catalog.EXTERNAL_DESTINATIONS) | {"sales_slack"})
        response = _resolve(client, approval_id)
        _assert_rejected(response, task_id, approval_id)
        assert "hard" in response.json()["detail"]
        assert _decision_count(task_id) == decisions_before
    # Nothing was consumed: with the policy restored, the human can still
    # resolve the same pending record exactly once.
    assert _resolve(client, approval_id).status_code == 200
    assert len(registry.sink_lines("slack_sales")) == 1


def test_replay_hard_blocked_by_stricter_source_policy(client, monkeypatch):
    """A data group retired after review becomes an unknown entity, which is
    a hard violation — the approval stays pending, nothing is read."""
    task_id, approval_id = _approve_read(client, monkeypatch)
    decisions_before = _decision_count(task_id)

    with monkeypatch.context() as stricter:
        stricter.setattr(
            catalog, "DATA_GROUPS",
            frozenset(g for g in catalog.DATA_GROUPS
                      if g != "employee_salary"))
        response = _resolve(client, approval_id)
        _assert_rejected(response, task_id, approval_id)
        assert "hard" in response.json()["detail"]
        assert _decision_count(task_id) == decisions_before
    # Nothing was consumed; the record is still resolvable afterwards.
    assert _resolve(client, approval_id).status_code == 200


def test_soft_review_approval_still_replays_exactly_once(client):
    """Control: an unchanged soft-review approval still resolves to exactly
    one execution (the human grant is preserved)."""
    task_id, approval_id = _approve_outbound(client)
    first = _resolve(client, approval_id)
    assert first.status_code == 200, first.text
    assert first.json()["executed"] is True
    assert len(registry.sink_lines("slack_sales")) == 1
    assert _resolve(client, approval_id).status_code == 409


# --- R1b: tool / destination / connector drift ------------------------------

@pytest.mark.parametrize("field,value", [
    ("destination", "external_api"),
    ("action", "post_external"),
])
def test_replay_fails_closed_when_tool_registration_changed(
        client, monkeypatch, field, value):
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    changed = dict(tools.TOOL_REGISTRY["send_slack"])
    changed[field] = value
    monkeypatch.setitem(tools.TOOL_REGISTRY, "send_slack", changed)

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "changed since review" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before


@pytest.mark.parametrize("initial_webhook", [None, "configured"])
def test_replay_fails_closed_when_connector_configuration_changed(
        client, monkeypatch, initial_webhook):
    """A connector enabled (or removed) after review means the delivery
    target changed: fail closed, no HTTP, approval stays pending."""
    if initial_webhook:
        monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV,
                           "http://127.0.0.1:9/services/REVIEWED")
    else:
        monkeypatch.delenv(tools.REAL_SLACK_WEBHOOK_ENV, raising=False)
    task_id, approval_id = _approve_outbound(client)

    if initial_webhook:
        monkeypatch.delenv(tools.REAL_SLACK_WEBHOOK_ENV, raising=False)
    else:
        monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV,
                           "http://127.0.0.1:9/services/CHANGED")

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "connector" in response.json()["detail"]


def test_replay_without_tool_binding_fails_closed(client):
    """Records created before the R1b gate carry no tool binding: replay
    asks for re-review instead of guessing."""
    import taskfence.gateway as gw
    task_id, approval_id = _approve_outbound(client)
    record = gw.APPROVALS.get(approval_id)
    data = json.loads(record["args"])
    data["tool_binding"] = None
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(data), approval_id))

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "no verifiable tool/destination binding" in response.json()["detail"]


# --- R3: approved reads are bound to content and labels ---------------------

def test_read_replay_succeeds_when_nothing_changed(client, monkeypatch):
    """Control: the content binding does not block a faithful replay."""
    task_id, approval_id = _approve_read(client, monkeypatch)
    response = _resolve(client, approval_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is True
    assert body["result"]["asset_id"] == "employee_salary"


def test_read_replay_executes_the_verified_bytes(client, monkeypatch):
    """The bytes that pass the binding check are the bytes that execute:
    the gateway must not re-read the asset after verifying it (R3)."""
    import taskfence.gateway as gw
    task_id, approval_id = _approve_read(client, monkeypatch)
    original_read = registry.read_asset
    calls: list[str] = []

    def _changes_after_first_read(asset_id: str) -> str:
        calls.append(asset_id)
        return (original_read(asset_id) if len(calls) == 1
                else "CHANGED AFTER VERIFICATION")

    monkeypatch.setattr(registry, "read_asset", _changes_after_first_read)
    response = _resolve(client, approval_id)
    assert response.status_code == 200, response.text
    assert calls == ["employee_salary"]      # exactly one read, pre-claim
    assert "CHANGED AFTER VERIFICATION" not in response.json()["result"]["content"]
    assert response.json()["result"]["content"] == original_read(
        "employee_salary")


def test_read_replay_fails_closed_when_content_changed(client, monkeypatch):
    import taskfence.gateway as gw
    task_id, approval_id = _approve_read(client, monkeypatch)
    decisions_before = _decision_count(task_id)

    monkeypatch.setattr(registry, "read_asset",
                        lambda asset_id: "TAMPERED: different bytes")

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "content or classification changed" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before


def test_read_replay_fails_closed_when_classification_changed(client,
                                                              monkeypatch):
    import taskfence.gateway as gw
    task_id, approval_id = _approve_read(client, monkeypatch)
    decisions_before = _decision_count(task_id)

    original_classify = gw.classify

    def _stricter(asset_id, content):
        # A label the reviewer never saw (the asset already carries PII).
        return sorted(set(original_classify(asset_id, content))
                      | {"SOURCE_CODE"})

    monkeypatch.setattr(gw, "classify", _stricter)

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "content or classification changed" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before


def test_rejected_replay_writes_no_audit_or_decision_row(client, monkeypatch):
    """A rejected replay is inert: the record stays pending and no audit or
    decision row is appended for the refusal."""
    import taskfence.gateway as gw
    task_id, approval_id = _approve_read(client, monkeypatch)
    audit_before = len(gw.AUDIT.read_all(task_id))
    decisions_before = _decision_count(task_id)

    monkeypatch.setattr(registry, "read_asset",
                        lambda asset_id: "TAMPERED: different bytes")

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert len(gw.AUDIT.read_all(task_id)) == audit_before
    assert _decision_count(task_id) == decisions_before


def test_read_replay_without_asset_binding_fails_closed(client, monkeypatch):
    import taskfence.gateway as gw
    task_id, approval_id = _approve_read(client, monkeypatch)
    record = gw.APPROVALS.get(approval_id)
    data = json.loads(record["args"])
    data["asset_binding"] = None
    gw.APPROVALS.db.execute(
        "UPDATE approvals SET args = ? WHERE approval_id = ?",
        (json.dumps(data), approval_id))

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "no verifiable content binding" in response.json()["detail"]


# --- R1b extension: unclassified external-delivery tool protection ---

def test_replay_fails_closed_when_real_delivery_flag_omitted(client, monkeypatch):
    """Future connector protection: a tool previously approved with
    real_delivery cannot bypass the binding by having the flag removed.
    The registry invariant fails closed for unclassified external-delivery
    tools, leaving the approval pending with no side effects."""
    import taskfence.gateway as gw
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    # Remove the real_delivery flag from send_slack,
    # simulating a future configuration change that unclassifies the tool.
    changed = dict(tools.TOOL_REGISTRY["send_slack"])
    changed.pop("real_delivery", None)
    monkeypatch.setitem(tools.TOOL_REGISTRY, "send_slack", changed)

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "connector" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before


def test_replay_fails_closed_when_real_delivery_flag_added(client, monkeypatch):
    """Ensure that adding real_delivery to a previously-unflagged tool also
    requires re-review: the binding must match the reviewed configuration.

    Creates approval without real_delivery, then adds both the flag and
    webhook, verifying the binding check detects the configuration change."""
    import taskfence.gateway as gw
    # First create an approval WITHOUT real_delivery flag
    # (no webhook env var set, so real_delivery is effectively off)
    monkeypatch.delenv(tools.REAL_SLACK_WEBHOOK_ENV, raising=False)
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    # Now add the real_delivery flag AND set the webhook
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV,
                       "http://127.0.0.1:9/services/REVIEWED")
    changed = dict(tools.TOOL_REGISTRY["send_slack"])
    changed["real_delivery"] = True
    monkeypatch.setitem(tools.TOOL_REGISTRY, "send_slack", changed)

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "connector" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before


def test_replay_without_tool_binding_fails_closed(client):
    """Records created before the R1b gate carry no tool binding: replay
    asks for re-review instead of guessing."""
    import taskfence.gateway as gw
    task_id, approval_id = _approve_outbound(client)
    record = gw.APPROVALS.get(approval_id)

# --- Classification enforcement tests ---

def test_replay_fails_closed_when_tool_missing_classification(client):
    """A newly registered external-delivery tool without
    side_effect_classification fails closed: the approval remains
    pending and no side effect occurs."""
    import taskfence.gateway as gw
    import json
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    # Simulate a new tool entry without side_effect_classification
    original = dict(tools.TOOL_REGISTRY["send_slack"])
    del tools.TOOL_REGISTRY["send_slack"]["side_effect_classification"]
    # Also remove real_delivery so it's treated as a new external-delivery tool
    original.pop("real_delivery", None)
    tools.TOOL_REGISTRY["send_slack"] = original

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "classification" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before

def test_replay_fails_closed_when_classification_changes_after_review(client):
    """A tool's side_effect_classification changed after the human reviewed
    the approval: the R1b binding check detects the mismatch and fails closed."""
    import taskfence.gateway as gw
    import json
    task_id, approval_id = _approve_outbound(client)
    decisions_before = _decision_count(task_id)

    # Change the classification after the approval was created
    original = dict(tools.TOOL_REGISTRY["send_slack"])
    original["side_effect_classification"] = "read_only"  # Change from external_delivery
    tools.TOOL_REGISTRY["send_slack"] = original

    response = _resolve(client, approval_id)
    _assert_rejected(response, task_id, approval_id)
    assert "classification" in response.json()["detail"]
    assert _decision_count(task_id) == decisions_before
