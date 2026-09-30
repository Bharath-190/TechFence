"""Optional real Slack integration tests (MCP extension kit, Person 4 / M4).

Real integration surface: EXACTLY ONE Slack incoming webhook, activated
only by the TASKFENCE_REAL_SLACK_WEBHOOK environment variable. The POST
itself lives on the gateway-executed ALLOW path (taskfence/tools.py
send_slack -> _post_real_slack), which gives the kit's required behavior
structurally:

- fake JSONL sinks stay the default (webhook unset -> zero HTTP calls);
- ALLOW   -> exactly one HTTP POST (plus the unchanged fake sink line);
- BLOCK   -> zero HTTP calls (the tool body never runs);
- APPROVE -> zero HTTP calls until a human resolves the approval; the
  sanctioned allow_once replay then executes exactly once.

Normal tests here NEVER contact the real network: every POST is captured
by a local HTTP recorder on 127.0.0.1 standing in for the webhook. The
one genuinely live test is marked `live_integration`, which pytest.ini
deselects by default. The webhook URL is treated as a secret: tests
assert it never leaks into tool results, fake sink records or stdout.
"""

import asyncio
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_scenario_d import real_gateway  # noqa: E402,F401

from taskfence import registry, tools  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_CMD = str(REPO_ROOT / ".venv" / "bin" / "python")
TASK_TEXT = "Summarize Q3 sales and post it to #sales."
ALLOW_ARGS = {"channel": "#sales",
              "text": "Q3: steady growth across all regions.",
              "source_assets": ["sales_report_q3"]}
APPROVE_ARGS = {"channel": "#sales", "text": "regional conversion: 42%",
                "source_assets": ["customer_db"]}
BLOCK_ARGS = {"url": "https://collect.example.invalid/ingest",
              "payload": "customer data", "source_assets": ["customer_db"]}


class _Recorder:
    def __init__(self):
        self.posts: list[tuple[str, bytes]] = []

    def handle(self, handler) -> None:
        length = int(handler.headers.get("Content-Length", 0))
        body = handler.rfile.read(length) if length else b""
        self.posts.append((handler.path, body))
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write(b"ok")


@pytest.fixture()
def local_webhook(monkeypatch):
    """A local HTTP server standing in for the Slack incoming webhook.
    Records every POST; reachable ONLY at 127.0.0.1 (no real network)."""
    recorder = _Recorder()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            recorder.handle(self)

        def log_message(self, *args):  # keep test output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    recorder.url = (f"http://127.0.0.1:{server.server_port}"
                    f"/services/T000/B000/SECRETXYZ")
    yield recorder
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch, tmp_path):
    """Every test starts with real Slack OFF and Ollama unreachable, and
    the repository outbox comes out byte-identical afterwards."""
    monkeypatch.delenv(tools.REAL_SLACK_WEBHOOK_ENV, raising=False)
    monkeypatch.setenv("TASKFENCE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")  # human-only path
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    registry.reset_outbox()
    yield
    registry.reset_outbox()


def _test_client():
    from fastapi.testclient import TestClient
    from taskfence.gateway import app
    return TestClient(app)


def _create_task(client) -> str:
    return client.post("/tasks",
                       json={"task_text": TASK_TEXT}).json()["task_id"]


# --- 1+2. fake/default behavior ----------------------------------------------


def test_default_is_fake_with_zero_http_calls():
    """Webhook unset (default): ALLOW still works, fake sink line written,
    ZERO HTTP calls, no real-Slack delivery note in the result."""
    with _test_client() as client:
        task_id = _create_task(client)
        body = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": "send_slack",
                                 "args": ALLOW_ARGS}).json()
    assert body["decision"]["outcome"] == "ALLOW", body
    assert "real_slack" not in body["result"]
    assert len(registry.sink_lines("slack_sales")) == 1  # fake sink intact


def test_allow_with_webhook_posts_exactly_once(local_webhook, monkeypatch):
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV, local_webhook.url)
    result = tools.send_slack(
        {"task_id": "task-x", "source_asset": "sales_report_q3",
         "content": ALLOW_ARGS["text"]}, **ALLOW_ARGS)
    assert result["delivered"] is True
    assert result["real_slack"] == "delivered"
    assert len(registry.sink_lines("slack_sales")) == 1  # fake still fires
    assert len(local_webhook.posts) == 1  # EXACTLY one HTTP POST (req. 7)
    assert json.loads(local_webhook.posts[0][1]) == {"text":
                                                     ALLOW_ARGS["text"]}


# --- 3+4. BLOCK and APPROVE send zero HTTP calls ------------------------------


def test_block_and_approve_send_zero_http_calls(local_webhook, monkeypatch):
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV, local_webhook.url)
    with _test_client() as client:
        task_id = _create_task(client)
        approved = client.post(
            f"/tasks/{task_id}/tool-call",
            json={"tool": "send_slack", "args": APPROVE_ARGS}).json()
        assert approved["decision"]["outcome"] == "APPROVE", approved
        assert approved["approval_id"]
        blocked = client.post(f"/tasks/{task_id}/tool-call",
                              json={"tool": "post_external",
                                    "args": BLOCK_ARGS}).json()
        assert blocked["decision"]["outcome"] == "BLOCK", blocked
    assert local_webhook.posts == []  # zero HTTP calls (req. 8 + 9)
    assert registry.sink_lines("slack_sales") == []  # nothing delivered


def test_allow_once_replay_posts_exactly_once(local_webhook, monkeypatch):
    """The ONLY sanctioned second execution: a human-approved allow_once."""
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV, local_webhook.url)
    with _test_client() as client:
        task_id = _create_task(client)
        approved = client.post(
            f"/tasks/{task_id}/tool-call",
            json={"tool": "send_slack", "args": APPROVE_ARGS}).json()
        assert local_webhook.posts == []  # held: nothing posted pre-approval
        resolved = client.post(
            f"/approvals/{approved['approval_id']}/resolve",
            json={"choice": "allow_once"},
            headers={"X-Admin-Token": "devtoken"}).json()
        assert resolved["status"] == "allowed_once", resolved
        assert resolved["executed"] is True
    assert len(local_webhook.posts) == 1  # exactly one POST, post-approval


# --- 5. secret hygiene --------------------------------------------------------


def test_webhook_url_never_leaks(local_webhook, monkeypatch, capsys):
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV, local_webhook.url)
    result = tools.send_slack(
        {"task_id": "task-x", "source_asset": "sales_report_q3",
         "content": ALLOW_ARGS["text"]}, **ALLOW_ARGS)
    secret = "SECRETXYZ"  # the credential-bearing part of any webhook URL
    assert secret not in json.dumps(result)               # tool result
    assert secret not in json.dumps(                      # fake sink record
        registry.sink_lines("slack_sales"))
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err      # never logged
    assert len(local_webhook.posts) == 1                  # POST went out


def test_real_slack_http_error_reports_status_not_url(monkeypatch):
    """A failed delivery surfaces an error note — never the URL, never a
    raised exception out of the executed ALLOW path."""

    def _boom(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV,
                       "http://127.0.0.1:9/services/T000/B000/SECRETXYZ")
    monkeypatch.setattr(tools.httpx, "post", _boom)
    result = tools.send_slack(
        {"task_id": "task-x", "source_asset": "sales_report_q3",
         "content": ALLOW_ARGS["text"]}, **ALLOW_ARGS)
    assert result["delivered"] is True                    # fake sink fired
    assert result["real_slack"] == "error_ConnectError"
    assert "SECRETXYZ" not in json.dumps(result)


# --- 6. MCP end-to-end: ALLOW posts exactly once through the real path --------


def test_mcp_allow_path_posts_exactly_once(real_gateway, local_webhook,
                                           monkeypatch):
    """Hermetic full chain: real MCP stdio server -> real uvicorn gateway
    -> ALLOW -> send_slack executes -> exactly one webhook POST, captured
    by the local recorder. No monkeypatching of the production path; the
    spawned adapter inherits the webhook env var like a real deployment."""
    monkeypatch.setenv(tools.REAL_SLACK_WEBHOOK_ENV, local_webhook.url)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHONPATH": str(REPO_ROOT),
           "TASKFENCE_URL": real_gateway,
           "TASKFENCE_MCP_TIMEOUT": "10",
           "TASKFENCE_REAL_SLACK_WEBHOOK": local_webhook.url,
           "TASKFENCE_OLLAMA_URL": "http://127.0.0.1:1"}

    async def _run() -> dict:
        params = StdioServerParameters(
            command=SERVER_CMD, args=["-m", "mcp_gateway.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("send_slack", {
                    "task_text": TASK_TEXT, "channel": "#sales",
                    "text": ALLOW_ARGS["text"],
                    "source_assets": ["sales_report_q3"]})
        return json.loads(result.content[0].text)

    outcome = asyncio.run(_run())
    assert outcome["decision"] == "ALLOW", outcome
    assert outcome["executed"] is True
    assert outcome["result"]["delivered"] is True
    assert outcome["result"]["real_slack"] == "delivered"
    assert len(local_webhook.posts) == 1  # exactly one POST over MCP too


# --- 7. the genuinely live test (deselected by default) -----------------------


@pytest.mark.live_integration
def test_live_real_slack_webhook_delivery():
    """Requires TASKFENCE_REAL_SLACK_WEBHOOK to point at a REAL Slack
    incoming webhook. Deselected by default; run explicitly with:
    pytest -m live_integration. Sends exactly one tiny POST."""
    webhook = tools.real_slack_webhook()
    if not webhook or "hooks.slack.com" not in webhook:
        pytest.skip("TASKFENCE_REAL_SLACK_WEBHOOK not set to a real webhook")
    registry.reset_outbox()
    try:
        result = tools.send_slack(
            {"task_id": "task-live", "source_asset": None,
             "content": "TaskFence M4 live integration check"},
            channel="#sales", text="TaskFence M4 live integration check")
        assert result["delivered"] is True
        assert result.get("real_slack") == "delivered", result
        assert len(registry.sink_lines("slack_sales")) == 1
    finally:
        registry.reset_outbox()
