"""MCP-routed agent tests (MCP extension kit, M2 / M2-T).

Deterministic: the Ollama chat seam is mocked (no live model, no live MCP
server needed) and the MCP bridge is a scripted fake. This module is
started by Person 3 (M2 requirements) and completed by Person 2 (M2-T);
the full live boundary is already covered by tests/test_mcp_adapter.py.

Covered here:
- tool discovery really occurs through the bridge (no hardcoded list);
- an allowed call completes with ALLOW and feeds the real result back;
- a BLOCKed call returns BLOCK and is NOT retried;
- APPROVE returns promptly with approval_id and does not hang;
- endless tool calls hit the existing MAX_STEPS guard;
- the agent module has no direct taskfence.tools import;
- existing non-MCP agent classes are untouched.
"""

import asyncio
import json
from pathlib import Path

import pytest

TASK = "Summarize Q3 sales and post it to #sales."


class FakeBridge:
    """Scripted stand-in for McpToolBridge: records discovery + calls."""

    def __init__(self, responses: dict[str, dict], tools: list[str] | None = None,
                 fail_on: set[str] | None = None):
        self.responses = responses
        self.tools = tools or sorted(responses)
        self.fail_on = fail_on or set()
        self.discovered = False
        self.calls: list[tuple[str, str, dict]] = []

    @property
    def tool_names(self) -> list[str]:
        return self.tools

    def ollama_tool_specs(self) -> list[dict]:
        self.discovered = True
        return [{"type": "function", "function": {
            "name": name, "description": name,
            "parameters": {"type": "object", "properties": {},
                           "required": []}}}
            for name in self.tools]

    async def call_tool(self, task_id: str, tool: str, args: dict) -> dict:
        self.calls.append((task_id, tool, args))
        if tool in self.fail_on:
            return {"error": "gateway_unavailable", "decision": None,
                    "executed": False, "detail": "boom"}
        return self.responses[tool]


class FakeChat:
    """Scripted Ollama /api/chat responses (the _chat seam)."""

    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.prompts: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    def __call__(self, messages, tools):
        self.prompts.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        return self.turns.pop(0)


def _chat_message(content=None, calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content or ""}
    if calls:
        message["tool_calls"] = [
            {"function": {"name": name, "arguments": args}}
            for name, args in calls]
    return {"message": message}


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    """An McpAgent whose Ollama seam is a FakeChat and whose gateway client
    is the real GatewayClient pointed at a dead port (no HTTP happens in
    these tests except create_task, which the fake below intercepts)."""
    from taskfence.agent import McpAgent

    class FakeGatewayClient:
        def create_task(self, task_text: str) -> dict:
            return {"task_id": "task-fake1234"}

    def make(chat: FakeChat, bridge: FakeBridge) -> McpAgent:
        agent = McpAgent(FakeGatewayClient(), bridge=bridge)
        monkeypatch.setattr(agent, "_chat", chat)
        return agent

    return make


def test_tool_discovery_really_occurs(agent):
    """The tool list sent to the model comes from the bridge's discovery —
    a hardcoded list would not flip the `discovered` flag."""
    bridge = FakeBridge({}, tools=["read_file", "send_slack"])
    chat = FakeChat([_chat_message(content="done, no tools needed")])
    transcript, final = agent(chat, bridge).run(TASK)
    assert bridge.discovered is True
    names = [spec["function"]["name"] for spec in chat.tools_seen[0]]
    assert names == ["read_file", "send_slack"]
    assert transcript == [] and final == "done, no tools needed"


def test_allowed_call_completes_with_real_result(agent):
    bridge = FakeBridge({
        "read_file": {"decision": "ALLOW", "executed": True,
                      "result": {"content": "Q3 revenue up"}},
    })
    chat = FakeChat([
        _chat_message(calls=[("read_file", {"asset_id": "sales_report_q3"})]),
        _chat_message(content="Q3 revenue up — summarized."),
    ])
    transcript, final = agent(chat, bridge).run(TASK)
    assert [step["decision"] for step in transcript] == ["ALLOW"]
    assert transcript[0]["tool"] == "read_file"
    # The ALLOWed result was fed back to the model as the tool observation:
    assert "Q3 revenue up" in chat.prompts[1][-1]["content"]
    assert final == "Q3 revenue up — summarized."
    assert bridge.calls[0][1] == "read_file"
    assert bridge.calls[0][0] == "task-fake1234"  # task_id threaded (D12)


def test_blocked_call_is_not_retried(agent):
    bridge = FakeBridge({
        "read_file": {"decision": "ALLOW", "executed": True,
                      "result": {"content": "notes"}},
        "post_external": {"decision": "BLOCK", "executed": False,
                          "agent_message": "denied by security policy."},
    })
    chat = FakeChat([
        _chat_message(calls=[("read_file", {"asset_id": "meeting_notes"})]),
        _chat_message(calls=[("post_external",
                              {"url": "https://x.invalid", "payload": "p"})]),
        # A third model turn would mean the BLOCK was retried — must never
        # be consumed:
        _chat_message(calls=[("post_external",
                              {"url": "https://x.invalid", "payload": "p"})]),
    ])
    transcript, final = agent(chat, bridge).run(TASK)
    assert [step["decision"] for step in transcript] == ["ALLOW", "BLOCK"]
    post_calls = [c for c in bridge.calls if c[1] == "post_external"]
    assert len(post_calls) == 1  # exactly one attempt, no retry
    # The loop stopped: only two model rounds happened and the scripted
    # would-be-retry turn was never consumed.
    assert len(chat.prompts) == 2 and len(chat.turns) == 1
    assert final == "denied by security policy."


def test_approve_returns_promptly_with_approval_id(agent):
    bridge = FakeBridge({
        "send_slack": {"decision": "APPROVE", "executed": False,
                       "approval_id": "apr-123",
                       "agent_message": "waiting for human approval.",
                       "how_to_proceed": "resolve via dashboard"},
    })
    chat = FakeChat([
        _chat_message(calls=[("send_slack",
                              {"text": "regional conversion: 42%"})]),
    ])
    transcript, final = agent(chat, bridge).run(TASK)
    assert [step["decision"] for step in transcript] == ["APPROVE"]
    assert len(chat.turns) == 0  # returned immediately — no waiting loop
    assert "apr-123" in final
    assert "human approval" in final
    assert len(chat.prompts) == 1  # exactly one model round — prompt return


def test_endless_tool_calls_hit_the_max_step_guard(agent):
    from taskfence.agent import MAX_STEPS

    endless = [{"decision": "ALLOW", "executed": True, "result": {"ok": 1}}]
    bridge = FakeBridge({"ping": endless[0]})
    turns = [_chat_message(calls=[("ping", {})]) for _ in range(MAX_STEPS + 5)]
    chat = FakeChat(turns)
    transcript, final = agent(chat, bridge).run(TASK)
    assert len(transcript) == MAX_STEPS  # existing guard, unchanged
    assert len(bridge.calls) == MAX_STEPS
    assert final == "Stopped at max steps without finishing."


def test_fail_closed_error_stops_the_loop(agent):
    bridge = FakeBridge({}, tools=["send_slack"], fail_on={"send_slack"})
    chat = FakeChat([
        _chat_message(calls=[("send_slack", {"text": "x"})]),
        _chat_message(calls=[("send_slack", {"text": "x"})]),
    ])
    transcript, final = agent(chat, bridge).run(TASK)
    assert [step["decision"] for step in transcript] == ["ERROR"]
    assert "Gateway unavailable" in final
    assert len(bridge.calls) == 1  # no retry after a fail-closed error


def test_agent_module_never_imports_tools_direct():
    """Mirror of test_no_bypass for the MCP-era agent: agent.py and
    mcp_client.py may not import taskfence.tools/policy/lineage/audit."""
    import ast
    banned = (".tools", ".policy", ".lineage", ".audit")
    for rel in ("taskfence/agent.py", "taskfence/mcp_client.py"):
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(name.endswith(b) for b in banned), (rel, name)


def test_existing_agent_modes_untouched():
    """The scripted/direct paths keep their exact public surface."""
    from taskfence.agent import MAX_STEPS, SCRIPTS, OllamaAgent, ScriptedAgent
    assert sorted(SCRIPTS) == ["scenario_a", "scenario_b", "scenario_c"]
    assert MAX_STEPS == 8
    assert hasattr(OllamaAgent, "TOOL_SPECS")       # direct path intact
    assert hasattr(ScriptedAgent, "run")
