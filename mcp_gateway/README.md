# TaskFence MCP Gateway (mcp_gateway/)

A local **stdio** MCP server that exposes five tools. It is an **adapter,
not a security engine**: every protected call is forwarded to the existing
TaskFence gateway through `GatewayClient`, and only the gateway's real
ALLOW / APPROVE / BLOCK decision — its reasons, lineage and audit trail —
is reported back to the MCP client. Design decisions: `specs/MCP_DECISIONS.md`.

## Startup

Requires a running TaskFence gateway (default `http://localhost:8000`):

```bash
# terminal 1 — the security authority
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/uvicorn taskfence.gateway:app --port 8000
# terminal 2 — the MCP adapter (speaks MCP over stdio)
.venv/bin/python -m mcp_gateway.server
```

Environment: `TASKFENCE_URL` (gateway base URL, default
`http://localhost:8000`), `TASKFENCE_MCP_TIMEOUT` (fail-closed gateway
timeout seconds, default 30).

## Tool listing (discoverability check)

With the server running under an MCP client, `tools/list` returns exactly:

| Tool | Gateway tool | Behavior |
|---|---|---|
| `read_file` | `read_file` | content only on ALLOW |
| `search_drive` | — (metadata only) | candidates, never contents; each read is a separate evaluated call |
| `query_crm` | `query_customer_db` | evaluated read |
| `send_slack` | `send_slack` | BLOCK → nothing delivered; APPROVE → approval_id, not sent |
| `post_external` | `post_external` | BLOCK → nothing sent; APPROVE → approval_id, not sent |

All tools accept `task_id` (existing TaskFence task) or `task_text` (a task
is created via the existing gateway endpoint); neither → the call fails
closed. Responses echo the gateway's decision verbatim.

## Optional: real Slack delivery (flag-gated, M4)

By default `send_slack` delivers only to the local fake sink
(`outbox/slack_sales.jsonl`) — no HTTP call is made. To also deliver
allowed messages to ONE real Slack incoming webhook, set the environment
variable `TASKFENCE_REAL_SLACK_WEBHOOK` to the webhook URL **in the
process that runs the TaskFence gateway** (the gateway executes the tool
on ALLOW; the adapter process never needs the secret):

```bash
# create the webhook in Slack first (Slack admin UI → Incoming Webhooks),
# then export it only in your shell — never commit it, never put it in
# .env files or docs:
export TASKFENCE_REAL_SLACK_WEBHOOK='https://hooks.slack.com/services/…'
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/uvicorn taskfence.gateway:app --port 8000
```

Behavior (unchanged decision logic — the same gateway/policy pipeline is
the only security authority, there is no second Slack-specific check):

- ALLOW → the fake sink line is written AND exactly one HTTP POST is sent
  to the webhook; the tool result adds a `real_slack: delivered` note
  (or `error_*` with a status/error name — never the URL) on failure.
- BLOCK → zero HTTP calls (the tool never executes).
- APPROVE → zero HTTP calls until a human resolves the approval; a human
  `allow_once` executes exactly once → exactly one POST.
- Webhook unset → everything behaves exactly as before (fake only).

The URL is a secret: it is never logged, never returned in any result,
and never written to the audit trail or the fake sink. Normal tests never
contact the real network (the real-HTTP paths run against a local
recorder); the one live test is `pytest -m live_integration` (deselected
by default) and requires the variable to point at a real
`hooks.slack.com` webhook.

## Quick MCP-client check

```bash
.venv/bin/python - <<'EOF'
from mcp.client.mcpclient import MCPClient   # SDK v2 client
# any MCP stdio client works: spawn `python -m mcp_gateway.server`,
# call tools/list and assert the five tools above.
EOF
```

## Security properties (same invariants as the rest of TaskFence)

- The adapter holds **no policy logic** and never imports
  `taskfence.policy`, `taskfence.lineage`, `taskfence.tools` or
  `taskfence.audit` (enforced by `tests/test_mcp_adapter.py`).
- It never holds the admin token; approval resolution stays human-only.
- If the gateway is unreachable the tools return a structured
  `gateway_unavailable` error — never a local decision, never an execution.
- Search results are candidates, not permission.
