"""Gateway integration tests (kit Prompt C-T).

Do NOT change source files from here; report bugs instead of fixing them.
"""

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate state WITHOUT chdir: data/ paths are CWD-relative.
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    import taskfence.gateway as gw
    from taskfence.audit import AuditLog
    from taskfence.lineage import LineageTracker
    monkeypatch.setattr(gw, "TRACKER",
                        LineageTracker(db_path=tmp_path / "lin.db"))
    monkeypatch.setattr(gw, "AUDIT", AuditLog(tmp_path / "audit.db"))
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


def _create_task(client, text="Summarize Q3 sales and post it to #sales."):
    response = client.post("/tasks", json={"task_text": text})
    assert response.status_code == 200
    return response.json()["task_id"]


def _call(client, task_id, tool, args):
    response = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": tool, "args": args})
    assert response.status_code == 200, response.text
    return response.json()


SCENARIO_A_ARGS = {"asset_id": "sales_report_q3", "channel": "#sales",
                   "text": "Q3: steady growth across all regions.",
                   "source_assets": ["sales_report_q3"],
                   "transformation": "summary"}
SCENARIO_B_ARGS = {"url": "https://collect.example.invalid/ingest",
                   "payload": "customer data",
                   "source_assets": ["customer_db"],
                   "transformation": "export"}


@pytest.mark.parametrize("tool,args,expected_decision,slack_lines,ext_lines", [
    ("send_slack", SCENARIO_A_ARGS, "ALLOW", 1, 0),
    ("post_external", SCENARIO_B_ARGS, "BLOCK", 0, 0),
    ("unknown_tool_xyz", {"asset_id": "sales_report_q3"}, "BLOCK", 0, 0),
])
def test_decision_and_sink_table(client, tool, args, expected_decision,
                                 slack_lines, ext_lines):
    task_id = _create_task(client)
    body = _call(client, task_id, tool, args)
    assert body["decision"]["outcome"] == expected_decision, body
    assert len(registry.sink_lines("slack_sales")) == slack_lines
    assert len(registry.sink_lines("external_api")) == ext_lines


def test_blocked_tool_is_never_invoked(client, monkeypatch):
    import taskfence.tools as tools_mod

    def _boom(*a, **k):
        raise AssertionError("post_external must not run on BLOCK")

    monkeypatch.setattr(tools_mod, "post_external", _boom)
    task_id = _create_task(client)
    body = _call(client, task_id, "post_external", SCENARIO_B_ARGS)
    assert body["decision"]["outcome"] == "BLOCK"
    assert body["result"] is None


def test_exactly_one_audit_event_per_call(client):
    task_id = _create_task(client)
    before = len(client.get("/audit").json()["events"])
    _call(client, task_id, "send_slack", SCENARIO_A_ARGS)
    after_allow = len(client.get("/audit").json()["events"])
    assert after_allow == before + 1
    _call(client, task_id, "post_external", SCENARIO_B_ARGS)
    after_block = len(client.get("/audit").json()["events"])
    assert after_block == after_allow + 1
    _call(client, task_id, "no_such_tool", {})
    after_unknown = len(client.get("/audit").json()["events"])
    assert after_unknown == after_block + 1  # I5 covers BLOCKs too


def test_unknown_tool_response_shape(client):
    task_id = _create_task(client)
    body = _call(client, task_id, "no_such_tool", {})
    assert body["decision"]["outcome"] == "BLOCK"
    assert body["agent_message"]  # DECISIONS §7 message present
    assert body["result"] is None


def test_out_of_scope_read_flag_false_allow_taint(client):
    task_id = _create_task(client)
    body = _call(client, task_id, "read_file",
                 {"path": "hr/employee_salary.csv"})
    assert body["decision"]["outcome"] == "ALLOW"
    assert body["tainted"] is True
    state = client.get(f"/tasks/{task_id}").json()
    assert "employee_salary" in state["session_reads"]


def test_out_of_scope_read_flag_true_approves(client, monkeypatch):
    import taskfence.gateway as gw
    monkeypatch.setattr(gw, "GATE_OUT_OF_SCOPE_READS", True)
    task_id = _create_task(client)
    body = _call(client, task_id, "read_file",
                 {"path": "hr/employee_salary.csv"})
    assert body["decision"]["outcome"] == "APPROVE"
    assert body["result"] is None


def test_in_scope_read_allows_without_taint(client):
    task_id = _create_task(client)
    body = _call(client, task_id, "read_file",
                 {"path": "drive/sales_report_q3.csv"})
    assert body["decision"]["outcome"] == "ALLOW"
    assert body["tainted"] is False
    assert "North,Anvil Pro" in body["result"]["content"]


def test_conservative_taint_blocks_later_outbound(client):
    """DECISIONS §4: after an out-of-scope read, even a clean-text outbound
    payload is tainted by session reads (may over-block; disclosed)."""
    task_id = _create_task(client)
    _call(client, task_id, "read_file", {"path": "hr/employee_salary.csv"})
    body = _call(client, task_id, "post_external", {
        "url": "https://collect.example.invalid/ingest",
        "payload": "all clear, nothing sensitive",
        "source_assets": ["sales_report_q3"],  # in-scope declared source
        "transformation": "summary"})
    assert body["decision"]["outcome"] == "BLOCK"
    assert len(registry.sink_lines("external_api")) == 0


def test_unknown_task_404(client):
    response = client.post("/tasks/nope/tool-call",
                           json={"tool": "read_file", "args": {}})
    assert response.status_code == 404


def test_lineage_endpoint_shows_scenario_c_chain(client):
    task_id = _create_task(client)
    _call(client, task_id, "read_file", {"path": "hr/employee_salary.csv"})
    lineage = client.get(f"/lineage/{task_id}").json()
    assert {"employee_salary"} <= {n["node"] for n in lineage["nodes"]}
