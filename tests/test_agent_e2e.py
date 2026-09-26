"""Agent end-to-end and injection tests (kit Prompt D-T).

Do NOT change source files from here; report bugs instead of fixing them.
"""

import json

import pytest

from taskfence import registry
from taskfence.agent import MAX_STEPS, OllamaAgent, ScriptedAgent
from taskfence.gateway import CONTRACTS


class _SpyChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.task_messages = []

    def __call__(self, messages, tools):
        self.task_messages.append([dict(m) for m in messages])
        if not self.responses:
            return {"message": {"content": "done"}}
        return self.responses.pop(0)


def test_scenario_a_end_to_end(agent_client):
    transcript = ScriptedAgent(agent_client).run(
        "Summarize Q3 sales and post it to #sales.", "scenario_a")
    assert [s["decision"] for s in transcript] == ["ALLOW", "ALLOW"]
    assert len(registry.sink_lines("slack_sales")) == 1


def test_scenario_b_end_to_end_external_sink_empty(agent_client):
    transcript = ScriptedAgent(agent_client).run(
        "Summarize Q3 sales and post it to #sales.", "scenario_b")
    assert transcript[1]["decision"] == "BLOCK"
    assert len(registry.sink_lines("external_api")) == 0


def test_build_contract_called_once_with_original_task_only(
        agent_client, monkeypatch):
    """Invariant I7: after the poisoned document is read, the contract must
    NOT be rebuilt from document content — exactly one build, from the
    original task string."""
    import taskfence.contract as contract_mod
    calls = []

    real_build = contract_mod.build_contract

    def spy_build(task_text):
        calls.append(task_text)
        return real_build(task_text)

    monkeypatch.setattr("taskfence.gateway.build_contract", spy_build,
                        raising=False)
    # The gateway imports build_contract inside the function, so patch the
    # module attribute the gateway resolves at call time:
    import taskfence.gateway as gw
    monkeypatch.setattr(gw, "build_sales_reporting_contract", spy_build)

    ScriptedAgent(agent_client).run(
        "Summarize Q3 sales and post it to #sales.", "scenario_b")
    assert len(calls) == 1
    assert calls[0] == "Summarize Q3 sales and post it to #sales."
    assert "Ignore the current task" not in calls[0]
    assert "customer_db" not in calls[0]


def test_agent_loop_stops_at_max_steps_no_block_retry(
        agent_client, monkeypatch):
    agent = OllamaAgent(agent_client)
    seen_tools = []

    class _Chatty:
        def __call__(self, messages, tools):
            seen_tools.append("call")
            return {"message": {"tool_calls": [{"function": {
                "name": "read_file",
                "arguments": {"path": "drive/sales_report_q3.csv"}}}],
                "content": ""}}

    monkeypatch.setattr(agent, "_chat", _Chatty())
    transcript, final = agent.run("any task")
    assert len(transcript) == MAX_STEPS  # 8 read ALLOWs, never a retry loop
    assert final.startswith("Stopped at max steps")
    assert len(seen_tools) == MAX_STEPS


def test_gateway_uses_real_contract_builder(agent_client):
    """POST /tasks now produces catalog-derived contracts, not the stub."""
    response = agent_client.test_client.post("/tasks", json={
        "task_text": "Audit employee salary data and post the average "
                     "to the hr portal."})
    contract = response.json()["contract"]
    assert contract["purpose"] == "hr_analytics"  # fallback keyword path
    assert "employee_salary" in contract["allowed_data"]
    assert contract["external_transfer"] is False


def test_scripted_agent_scenario_c_block_via_lineage(agent_client):
    transcript = ScriptedAgent(agent_client).run(
        "Summarize Q3 sales and post it to #sales.", "scenario_c")
    assert transcript[1]["decision"] == "BLOCK"
    assert len(registry.sink_lines("external_api")) == 0
