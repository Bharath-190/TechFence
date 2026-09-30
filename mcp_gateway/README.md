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
