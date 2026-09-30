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

M4 (optional real Slack): send_slack additionally performs exactly ONE real
HTTP POST to the single Slack incoming webhook from
TASKFENCE_REAL_SLACK_WEBHOOK — but ONLY on the gateway-executed ALLOW path
(this function body runs only after an ALLOW decision). BLOCK and APPROVE
never reach it, so they perform zero HTTP calls. Fake behavior (the JSONL
sink) is unchanged and stays the default when the variable is unset. The
webhook URL is a secret: never logged, never returned, never audited.
"""

import json
import os

import httpx

from taskfence import registry

REAL_SLACK_WEBHOOK_ENV = "TASKFENCE_REAL_SLACK_WEBHOOK"
# Fail-closed POST timeout: a real delivery attempt must not hang the
# gateway response, and any HTTP failure is reported in the tool result
# WITHOUT the webhook URL (detail carries status/error text only).
REAL_SLACK_TIMEOUT_SECONDS = 5.0


def real_slack_webhook() -> str:
    """The configured real webhook URL, or '' when real Slack is disabled
    (fake sinks remain the default, kit M4 requirement 1/2)."""
    return os.environ.get(REAL_SLACK_WEBHOOK_ENV, "").strip()


def _post_real_slack(text: str) -> dict:
    """Exactly one real Slack incoming-webhook POST (kit M4 req. 7). Uses
    the existing httpx dependency only (req. 4). Returns a secret-free
    delivery note; the URL itself is never echoed, logged or audited
    (req. 10/11). Never raises: gateway execution already succeeded."""
    webhook = real_slack_webhook()
    if not webhook:
        return {}
    try:
        response = httpx.post(webhook, json={"text": text},
                              timeout=REAL_SLACK_TIMEOUT_SECONDS)
        return {"real_slack": "delivered" if response.status_code == 200
                else f"error_status_{response.status_code}"}
    except httpx.HTTPError as error:
        return {"real_slack": f"error_{type(error).__name__}"}

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
    # M4: exactly one real-webhook POST on this ALLOW-executed path; no
    # webhook configured -> zero HTTP calls (fake default, req. 1/2).
    result = {"delivered": True, "channel": channel, "sink": "slack_sales"}
    result.update(_post_real_slack(text))
    return result


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
