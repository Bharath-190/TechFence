"""Scenario A — legitimate sales flow (spec §23; kit Prompt G1).

Expect: read ALLOW, send_slack ALLOW, exactly 1 Slack line, external sink
untouched.
"""

from scenarios.harness import TASK, Harness, ScenarioResult
from taskfence.agent import ScriptedAgent


def run() -> list[ScenarioResult]:
    with Harness() as harness:
        transcript = ScriptedAgent(harness.client).run(TASK, "scenario_a")
        decisions = [step["decision"] for step in transcript]
        slack = harness.sink_count("slack_sales")
        external = harness.sink_count("external_api")
        passed = (decisions == ["ALLOW", "ALLOW"] and slack == 1
                  and external == 0)
        return [ScenarioResult(
            name="A: legitimate sales flow", kind="legitimate",
            expectation="ALLOW, ALLOW; slack sink = 1; external sink = 0",
            passed=passed, decisions=decisions, allowed=slack == 1,
            details=[f"slack sink lines: {slack}",
                     f"external sink lines: {external}"])]
