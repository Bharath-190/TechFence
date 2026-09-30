"""Local stdio MCP server adapting five tools onto the TaskFence gateway.

Architecture (specs/MCP_DECISIONS.md):

    MCP client (e.g. taskfence.agent --mcp, M2)
        ↓  MCP stdio
    this server  (adapter ONLY — no policy, no classification, no lineage)
        ↓  GatewayClient / HTTP (the same boundary every agent uses)
    TaskFence gateway  (the ONLY security authority)
        ↓  real Decision + audit + gateway-side execution
    ALLOW / APPROVE / BLOCK

Ground rules enforced here (kit M0/M1 guardrails):
- No ALLOW/APPROVE/BLOCK logic exists in this file: outcomes are returned
  verbatim from gateway responses (D4/D13).
- Protected operations are never executed locally; on ALLOW the GATEWAY has
  already executed the tool before responding (D4).
- search_drive is discovery over registry METADATA only; it never returns
  file contents and never calls registry.read_asset (D7).
- APPROVE is returned immediately with approval_id; no waiting, no retry (D8).
- If the gateway cannot provide a real Decision, the tool fails closed with
  a structured error and no outcome field (D13).
- Task/session mapping is stateless: args carry task_id or task_text (D12).

Run locally:  python -m mcp_gateway.server
(requires a running TaskFence gateway; see mcp_gateway/README.md)
"""

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from taskfence.client import GatewayClient
from taskfence.registry import ASSETS  # METADATA ONLY (D7) — never read_asset

GATEWAY_URL = os.environ.get("TASKFENCE_URL", "http://localhost:8000")
# Fail-closed default timeout: a hung gateway must surface as an error
# result, not as a silent success (D13). Generous, not tunable to infinity.
GATEWAY_TIMEOUT = float(os.environ.get("TASKFENCE_MCP_TIMEOUT", "30"))

server = MCPServer(
    name="taskfence-mcp-gateway",
    instructions=(
        "TaskFence security gateway adapter. Every tool call is evaluated by "
        "the TaskFence gateway against the task's contract. Pass either "
        "task_id (from a previous call's response) or task_text (a new task "
        "is created). APPROVE means a human must resolve the approval; the "
        "operation was NOT executed. BLOCK means denied. Errors mean no "
        "decision could be obtained — nothing was executed."),
)

# ---------------------------------------------------------------------------
# Internal helpers (adapter plumbing only — no security logic)
# ---------------------------------------------------------------------------


class _GatewayUnavailable(RuntimeError):
    """Raised when no real TaskFence Decision can be obtained (D13)."""


def _client() -> GatewayClient:
    return GatewayClient(base_url=GATEWAY_URL, timeout=GATEWAY_TIMEOUT)


def _task_id(args: dict[str, Any]) -> str:
    """D12: stateless task threading. task_id wins; else task_text creates
    one via the existing endpoint; neither -> fail closed."""
    task_id = str(args.get("task_id") or "")
    if task_id:
        return task_id
    task_text = str(args.get("task_text") or "")
    if not task_text:
        raise _GatewayUnavailable(
            "no task_id and no task_text: refusing to evaluate a request "
            "outside a TaskFence task (fail closed)")
    try:
        created = _client().create_task(task_text)
    except Exception as error:  # gateway down/timeout/non-200 -> fail closed
        raise _GatewayUnavailable(
            f"TaskFence gateway unavailable, no decision possible: {error}")
    return str(created["task_id"])


def _evaluate(task_id: str, tool: str, tool_args: dict[str, Any]) -> dict:
    """One protected call through the gateway. Returns the sanitized
    GatewayClient view (decision, agent_message, result, approval_id)."""
    try:
        return _client().call_tool(task_id, tool, tool_args)
    except Exception as error:
        raise _GatewayUnavailable(
            f"TaskFence gateway unavailable, no decision possible: {error}")


def _tool_result(response: dict) -> dict:
    """The REAL gateway outcome, verbatim (MCP guardrails 2/4/6). No outcome
    munging, no reason rewriting, no local approval logic."""
    out: dict[str, Any] = {
        "decision": response.get("decision"),
        "executed": response.get("decision") == "ALLOW",
        "agent_message": response.get("agent_message", ""),
    }
    if response.get("result") is not None:
        out["result"] = response["result"]
    if response.get("approval_id"):
        out["approval_id"] = response["approval_id"]
        out["executed"] = False  # APPROVE is not ALLOW (D8)
        out["how_to_proceed"] = (
            "A human must resolve this approval via the TaskFence dashboard "
            "or admin API (allow_once / expand_task / deny). This call did "
            "not execute and will not be retried automatically.")
    return out


def _fail_closed(error: Exception) -> dict:
    """D13: structured error, no outcome field, nothing executed."""
    return {"error": "gateway_unavailable",
            "detail": str(error),
            "executed": False,
            "decision": None}


# ---------------------------------------------------------------------------
# The five MCP tools (D5)
# ---------------------------------------------------------------------------


@server.tool(
    title="Read a registered drive file",
    description=("Read a registered enterprise file. Content is returned "
                 "only if the TaskFence gateway ALLOWS the read for the "
                 "current task; otherwise the real BLOCK/APPROVE decision "
                 "is returned."),
)
def read_file(task_id: str = "", task_text: str = "", path: str = "",
              asset_id: str = "") -> dict:
    """Gateway-evaluated file read. `asset_id` maps to the registry id;
    `path` is matched by filename exactly like the gateway's own resolver."""
    try:
        task = _task_id({"task_id": task_id, "task_text": task_text})
        tool_args: dict[str, Any] = {}
        if asset_id:
            tool_args["asset_id"] = asset_id
        if path:
            tool_args["path"] = path
        return _tool_result(_evaluate(task, "read_file", tool_args))
    except _GatewayUnavailable as error:
        return _fail_closed(error)


@server.tool(
    title="Search the drive catalog (discovery only)",
    description=("Search registered file metadata (name, path, source "
                 "system, data group). Returns CANDIDATES ONLY — never file "
                 "contents. Reading any candidate requires a separate "
                 "read_file call, individually evaluated by TaskFence."),
)
def search_drive(task_id: str = "", task_text: str = "",
                 query: str = "") -> dict:
    """D7: metadata listing over registry.ASSETS. Content access is never
    bundled into search results — search is not permission."""
    _ = _task_id({"task_id": task_id, "task_text": task_text}) or ""
    query_terms = query.lower().split()
    candidates = []
    for asset_id, (rel_path, source, group, _labels) in sorted(
            ASSETS.items()):
        haystack = f"{asset_id} {rel_path} {source} {group}".lower()
        if all(term in haystack for term in query_terms):
            candidates.append({"asset_id": asset_id, "path": rel_path,
                               "source": source, "data_group": group})
    return {"candidates": candidates, "count": len(candidates),
            "note": "Discovery only. Use read_file to request contents — "
                    "each read is individually evaluated by TaskFence."}


@server.tool(
    title="Query the customer database",
    description=("Run a query against the customer DB through the TaskFence "
                 "gateway. Evaluated against the task contract; results are "
                 "returned only on ALLOW."),
)
def query_crm(task_id: str = "", task_text: str = "", query: str = "") -> dict:
    """D6: rename wrapper over the existing query_customer_db gateway tool."""
    try:
        task = _task_id({"task_id": task_id, "task_text": task_text})
        return _tool_result(_evaluate(task, "query_customer_db",
                                      {"query": query}))
    except _GatewayUnavailable as error:
        return _fail_closed(error)


@server.tool(
    title="Send a Slack message (fake sink)",
    description=("Send a message to the sales Slack through the TaskFence "
                 "gateway. BLOCKed messages are never delivered. APPROVE "
                 "returns an approval_id and does NOT send."),
)
def send_slack(task_id: str = "", task_text: str = "", channel: str = "",
               text: str = "", source_assets: list[str] | None = None) -> dict:
    """Outbound flow: evaluated with declared sources (D5); the gateway's
    conservative taint applies exactly as for any other agent."""
    try:
        task = _task_id({"task_id": task_id, "task_text": task_text})
        tool_args: dict[str, Any] = {"text": text}
        if channel:
            tool_args["channel"] = channel
        if source_assets:
            tool_args["source_assets"] = source_assets
        return _tool_result(_evaluate(task, "send_slack", tool_args))
    except _GatewayUnavailable as error:
        return _fail_closed(error)


@server.tool(
    title="Post to an external API (fake sink)",
    description=("Post a payload to the external API through the TaskFence "
                 "gateway. Restricted data is BLOCKed via classification "
                 "and lineage. APPROVE returns an approval_id and does NOT "
                 "post."),
)
def post_external(task_id: str = "", task_text: str = "", url: str = "",
                  payload: str = "",
                  source_assets: list[str] | None = None) -> dict:
    try:
        task = _task_id({"task_id": task_id, "task_text": task_text})
        tool_args: dict[str, Any] = {"payload": payload}
        if url:
            tool_args["url"] = url
        if source_assets:
            tool_args["source_assets"] = source_assets
        return _tool_result(_evaluate(task, "post_external", tool_args))
    except _GatewayUnavailable as error:
        return _fail_closed(error)


if __name__ == "__main__":
    server.run()  # stdio (D2) — blocks until the client closes the session
