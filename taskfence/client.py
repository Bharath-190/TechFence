"""Agent-side gateway client (spec §15, §26; kit Prompt C3).

GatewayClient is the ONLY taskfence module agent code may import
(invariant I1, enforced statically by tests/test_no_bypass.py). It never
sees taskfence.tools, never holds the admin token (Phase E), and returns a
sanitized view: decision outcome + agent-facing message + its own result.
Full reasons/lineage/explanations stay human-side (DECISIONS §7).
"""

import os

import httpx

DEFAULT_BASE_URL = os.environ.get("TASKFENCE_URL", "http://localhost:8000")


class GatewayClient:
    """HTTP client for the TaskFence gateway. Agent-facing only."""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def create_task(self, task_text: str) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/tasks",
                                   json={"task_text": task_text})
            response.raise_for_status()
            return response.json()

    def call_tool(self, task_id: str, tool: str,
                  args: dict | None = None) -> dict:
        """Submit a tool request through the gateway. Returns a sanitized
        dict: decision, agent_message, and the tool result if executed."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/tasks/{task_id}/tool-call",
                json={"tool": tool, "args": args or {}})
            response.raise_for_status()
            body = response.json()
        return {"decision": body["decision"]["outcome"],
                "agent_message": body["agent_message"],
                "result": body.get("result"),
                "tainted": body.get("tainted", False),
                "approval_id": body.get("approval_id")}

    def task_state(self, task_id: str) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/tasks/{task_id}")
            response.raise_for_status()
            return response.json()


def decision_of(response: dict) -> str:
    """Convenience accessor used by the agents."""
    return response["decision"]
