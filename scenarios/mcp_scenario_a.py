"""MCP Scenario A — legitimate sales flow over the REAL MCP boundary.

Same flow as the scripted Scenario A, driven through the real MCP stdio
adapter: read the sales report (ALLOW) then send the Slack message
(ALLOW) — exactly one Slack sink line, external sink untouched. The MCP
layer adds no policy logic; the gateway's real ALLOW decisions, the
existing audit trail and the byte-level outbox evidence are what this
scenario reports (kit P4-M3).

collect() returns (ScenarioResult, evidence rows); run() is the
scenarios-suite view (results only), like every other scenario module.
"""

from scenarios.harness import ScenarioResult
from scenarios.mcp_runner import MCPRunner, as_scenario_result, \
    phase_rows
from taskfence.agent import SCRIPTS


def collect() -> tuple[ScenarioResult, list]:
    script = SCRIPTS["scenario_a"]
    with MCPRunner() as runner:
        # Deterministic discovery-first shape (guardrail: MCP tools are
        # discovered, never hardcoded into the flow).
        discovered = runner.discover_tools()
        snapshots = runner.sinks_before()
        rows = runner.run_script(script)
        deltas = runner.sink_deltas(snapshots)
    decisions = [row.decision for row in rows]
    audit = phase_rows("MCP A", rows)
    passed = (decisions == ["ALLOW", "ALLOW"]
              and deltas.get("slack_sales") == 1
              and deltas.get("external_api") == 0
              and discovered == {"read_file", "search_drive", "query_crm",
                                 "send_slack", "post_external"})
    return as_scenario_result(
        "MCP A: legitimate sales flow over MCP",
        "legitimate",
        "ALLOW (read), ALLOW (send); slack sink +1; external sink +0",
        decisions=decisions, sink_deltas=deltas, audit_rows=audit,
        passed=passed), audit


def run() -> list[ScenarioResult]:
    return [collect()[0]]
