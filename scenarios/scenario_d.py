"""Scenario D — scope expansion (spec §16; kit Prompt G2).

Expect: customer-data request to the allowed internal Slack APPROVEs;
human resolves allow_once; the message executes exactly once. This is the
"agent legitimately needs more data" story from spec §26 Q3.
"""

from scenarios.harness import TASK, Harness, ScenarioResult


def run() -> list[ScenarioResult]:
    results = []
    with Harness() as harness:
        call = harness.client.call_tool(TASK and _task_id(harness),
                                        "send_slack",
                                        {"channel": "#sales",
                                         "text": "regional conversion: 42%",
                                         "source_assets": ["customer_db"]})
        decisions = [call["decision"]]
        approved = decisions == ["APPROVE"]
        approval_id = call.get("approval_id")
        resolved = harness.test_client.post(
            f"/approvals/{approval_id}/resolve",
            json={"choice": "allow_once"},
            headers={"X-Admin-Token": "devtoken"}).json()
        executed_once = resolved.get("executed") is True
        single_use = harness.sink_count("slack_sales") == 1
        repeat = harness.client.call_tool(_task_id(harness), "send_slack",
                                          {"channel": "#sales",
                                           "text": "regional conversion: 42%",
                                           "source_assets": ["customer_db"]})
        reapproved = repeat["decision"] == "APPROVE"
        passed = approved and executed_once and single_use and reapproved
        results.append(ScenarioResult(
            name="D: scope expansion via allow_once", kind="legitimate",
            expectation="APPROVE -> allow_once executes once -> re-APPROVE",
            passed=passed, decisions=decisions + ["APPROVE"],
            allowed=executed_once,
            details=[f"approval executed: {executed_once}",
                     f"slack sink lines: {harness.sink_count('slack_sales')}",
                     f"repeat request APPROVEs again: {reapproved}"]))

    # expand_task variant: new contract version, old unchanged (I3).
    with Harness() as harness:
        call = harness.client.call_tool(_task_id(harness), "send_slack",
                                        {"channel": "#sales",
                                         "text": "regional conversion: 42%",
                                         "source_assets": ["customer_db"]})
        approval_id = call.get("approval_id")
        before = harness.test_client.get(
            f"/tasks/{_task_id(harness)}").json()["contract"]
        expanded = harness.test_client.post(
            f"/approvals/{approval_id}/resolve",
            json={"choice": "expand_task"},
            headers={"X-Admin-Token": "devtoken"}).json()
        new_contract = expanded["contract"]
        version_changed = new_contract["version"] != before["version"]
        parent_linked = (new_contract["parent_contract_id"]
                         == before["contract_id"])
        widened = ("customer_db" in new_contract["allowed_data"]
                   and "customer_db" not in before["allowed_data"])
        passed = (call["decision"] == "APPROVE" and version_changed
                  and parent_linked and widened)
        results.append(ScenarioResult(
            name="D2: expand_task creates a new contract version",
            kind="control",
            expectation="APPROVE -> expand_task -> new version, old intact",
            passed=passed, decisions=[call["decision"], "APPROVE"],
            allowed=False,
            details=[f"version changed: {version_changed}",
                     f"parent linked: {parent_linked}",
                     f"scope widened: {widened}"]))
    return results


def _task_id(harness) -> str:
    response = harness.client.create_task(
        "Summarize Q3 sales and post it to #sales.")
    return response["task_id"]
