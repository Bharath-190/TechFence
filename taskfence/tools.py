"""Fake enterprise tools (spec §14, §18; kit Prompt C1).

PRIVATE to the gateway: taskfence/agent.py and taskfence/client.py must never
import this module (invariant I1, enforced by tests/test_no_bypass.py).

Tool functions take a gateway-injected `ctx` holding gateway-verified values,
plus raw **args in the agent-supplied shape:
  ctx = {
    "task_id": str,
    "source_asset": str | None,  # registry asset id resolved by the gateway
    "content": str,              # content the gateway decided may move
  }
Sinks receive exactly one JSONL line per executed call.
"""

import json

from taskfence import registry

# tool name -> metadata (gateway resolves actions/destinations from this).
TOOL_REGISTRY = {
    "read_file": {
        "action": "read", "destination": "",
        "is_external": False, "is_outbound": False,
    },
    "query_customer_db": {
        "action": "query", "destination": "",
        "is_external": False, "is_outbound": False,
    },
    "send_slack": {
        "action": "send_message", "destination": "sales_slack",
        "is_external": False, "is_outbound": True,
    },
    "post_external": {
        "action": "post_external", "destination": "external_api",
        "is_external": True, "is_outbound": True,
    },
}


def _append_line(sink: str, record: dict) -> None:
    path = registry.OUTBOX_DIR / registry.SINKS[sink]
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_file(ctx: dict, **args) -> dict:
    asset_id = ctx["source_asset"]
    content = registry.read_asset(asset_id)
    return {"asset_id": asset_id, "path": str(args.get("path", "")),
            "content": content}


def query_customer_db(ctx: dict, **args) -> dict:
    query = str(args.get("query", "")).strip().lower()
    payload = json.loads(registry.read_asset("customer_db"))
    customers = payload.get("customers", [])
    if not query:
        matches = customers
    else:
        terms = query.split()
        matches = [c for c in customers
                   if all(term in " ".join(str(v) for v in c.values()).lower()
                          for term in terms)]
    return {"query": args.get("query", ""), "count": len(matches),
            "results": matches}


def send_slack(ctx: dict, **args) -> dict:
    channel = str(args.get("channel", "#sales"))
    text = ctx["content"]
    _append_line("slack_sales", {
        "channel": channel, "text": text, "task_id": ctx.get("task_id", ""),
        "asset_id": ctx.get("source_asset") or ""})
    return {"delivered": True, "channel": channel, "sink": "slack_sales"}


def post_external(ctx: dict, **args) -> dict:
    url = str(args.get("url", ""))
    payload_text = ctx["content"]
    _append_line("external_api", {
        "url": url, "payload": payload_text,
        "task_id": ctx.get("task_id", ""),
        "asset_id": ctx.get("source_asset") or ""})
    return {"sent": True, "url": url, "sink": "external_api"}


TOOL_FUNCTIONS = {
    "read_file": read_file,
    "query_customer_db": query_customer_db,
    "send_slack": send_slack,
    "post_external": post_external,
}
