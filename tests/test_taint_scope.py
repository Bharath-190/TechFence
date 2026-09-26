"""FIX-D5-T taint-scope tests (kit Prompt FIX-D5-T, Option A recorded).

Decision under test (specs/DECISIONS.md §3/§4, CONFIRMED): conservative
session taint STAYS — session reads remain implicit outbound source context
and label contributors; Scenario G remains a disclosed false positive.

Structure:
- Tests 1-2 are SYNTHETIC: permissive contracts that would ALLOW the flow if
  lineage were ignored, so a BLOCK can only come from the restricted-lineage
  check (restricted_label_escape) — never from contract-level external
  denial, unknown entities, or keyword scans. They fail if lineage
  inheritance is removed.
- Tests 3-4 run the REAL Scenario B/C demos and their side effects.
- Test 5 asserts the Option A conservative behavior end-to-end (a
  no-declared-source outbound still draws authorization context from
  session_reads) plus the FIX1 fail-closed expand interaction.
- Test 6 (kit item 6, Option B no-implicit-source) is intentionally NOT
  asserted: Option B was not chosen; test 5 asserts the OPPOSITE (current)
  behavior so any future silent change to Option B semantics fails here.
- Test 7 pins the Scenario G outcome to the recorded decision (APPROVE,
  disclosed false positive — results.md stays honest).
- Test 8 proves session reads remain auditable regardless of later flows.

No source file is modified by this test prompt.
"""

import pytest

from taskfence import catalog, registry
from taskfence.gateway import app
from taskfence.lineage import LineageTracker
from taskfence.models import FlowRequest, TaskContract
from taskfence.policy import PolicyEngine

ADMIN = {"X-Admin-Token": "devtoken"}

NOW = "2026-09-30T14:00:00Z"


def _permissive_external_contract(data_group: str) -> TaskContract:
    """Synthetic contract that otherwise PERMITS the external flow: the only
    thing that can block is the restricted-lineage check."""
    return TaskContract(
        contract_id="c-synthetic",
        purpose="sales_reporting",
        allowed_data=[data_group],
        allowed_destinations=["external_api"],
        allowed_actions=["read", "summarize", "post_external"],
        external_transfer=True,
        created_at=NOW,
    )


# --- 1. Synthetic Scenario B lineage test ------------------------------------

def test_synthetic_b_blocks_via_restricted_lineage_not_contract():
    engine = PolicyEngine()
    contract = _permissive_external_contract("customer_db")
    request = FlowRequest(
        tool="post_external", action="post_external",
        source="customer_db", source_group="customer_db",
        destination="external_api",
        labels=frozenset(registry.base_labels("customer_db")),
        transformation="export", payload="customer records export")
    decision = engine.evaluate(contract, request)
    assert decision.outcome == "BLOCK", decision.reasons
    # Caused by the restricted-label escape check, and nothing else:
    assert "restricted_label_escape" in decision.failed_checks
    assert "unknown_entity" not in decision.failed_checks
    assert "external_destination_forbidden" not in decision.failed_checks
    assert "outbound_action_out_of_scope" not in decision.failed_checks


# --- 2. Synthetic Scenario C lineage test ------------------------------------

def test_synthetic_c_blocks_via_inherited_labels_without_keywords(tmp_path):
    engine = PolicyEngine()
    tracker = LineageTracker(db_path=tmp_path / "lin.db",
                             mode="conservative")
    contract = _permissive_external_contract("employee_salary")
    # employee_salary -> salary_summary in the lineage tracker.
    tracker.register_asset("t-c", "employee_salary")
    tracker.derive("t-c", ["employee_salary"], "average", "salary_summary")
    # Labels come from the tracker's inheritance, NOT from a keyword scan:
    effective = tracker.effective_labels("t-c", "salary_summary",
                                         include_session=False)
    assert "EMPLOYEE_DATA" in effective  # inherited from the ancestor
    # The derived node has NO own labels — inheritance is the only source:
    assert tracker._node_labels("t-c", "salary_summary") == set()
    request = FlowRequest(
        tool="post_external", action="post_external",
        source="salary_summary", source_group="employee_salary",
        destination="external_api",
        labels=frozenset(effective),
        transformation="average",
        payload="Quarterly aggregate: 42")  # NO salary/CTC/comp keywords
    decision = engine.evaluate(contract, request)
    assert decision.outcome == "BLOCK", decision.reasons
    assert "restricted_label_escape" in decision.failed_checks
    assert "unknown_entity" not in decision.failed_checks
    # Must FAIL if lineage inheritance is removed: with no inherited labels
    # the identical request would be unrestricted and ALLOW under the
    # permissive contract.
    stripped = request.model_copy(update={"labels": frozenset(
        tracker._node_labels("t-c", "salary_summary"))})
    assert engine.evaluate(contract, stripped).outcome == "ALLOW"
    tracker.close()


# --- 3 + 4. Real Scenario B and C demos remain BLOCK --------------------------

def test_scenario_b_demo_still_blocks_external_sink_untouched():
    from scenarios.harness import TASK, Harness
    from taskfence.agent import ScriptedAgent
    with Harness() as harness:
        transcript = ScriptedAgent(harness.client).run(TASK, "scenario_b")
        decisions = [step["decision"] for step in transcript]
        assert decisions == ["ALLOW", "BLOCK"], decisions
        assert harness.sink_count("external_api") == 0


def test_scenario_c_demo_still_blocks_keyword_free_lineage():
    from scenarios.harness import TASK, Harness
    from taskfence.agent import ScriptedAgent
    with Harness() as harness:
        transcript = ScriptedAgent(harness.client).run(TASK, "scenario_c")
        decisions = [step["decision"] for step in transcript]
        assert decisions == ["ALLOW", "BLOCK"], decisions
        assert harness.sink_count("external_api") == 0


# --- 5. Option A: conservative implicit-source behavior + FIX1 interaction ----

def test_option_a_no_declared_source_still_uses_session_context():
    """Recorded conservative behavior: an outbound with NO declared source
    may still draw source-group context from session_reads (soft violation,
    APPROVE at an allowed internal destination) — NOT default-deny BLOCK."""
    from scenarios.harness import TASK, Harness
    with Harness() as harness:
        task_id = harness.client.create_task(TASK)["task_id"]
        harness.client.call_tool(task_id, "read_file",
                                 {"path": "hr/employee_salary.csv"})
        # Raw client: the agent-facing one sanitizes the decision dict.
        response = harness.test_client.post(
            f"/tasks/{task_id}/tool-call",
            json={"tool": "send_slack",
                  "args": {"channel": "#sales",
                           "text": "clean internal note"}}).json()
        assert response["decision"]["outcome"] == "APPROVE", response
        assert "source_outside_contract" in \
            response["decision"]["failed_checks"]
        assert harness.sink_count("slack_sales") == 0
        # FIX1 interaction recorded in DECISIONS §4: expand_task on a
        # request with no declared source group FAILS CLOSED and the
        # approval stays pending for the human.
        approval_id = response.get("approval_id")
        assert approval_id
        result = harness.test_client.post(
            f"/approvals/{approval_id}/resolve",
            json={"choice": "expand_task"},
            headers={"X-Admin-Token": "devtoken"})
        assert result.status_code == 409
        import taskfence.gateway as gw
        assert gw.APPROVALS.get(approval_id)["status"] == "pending"


# --- 7. Scenario G outcome matches the recorded decision ----------------------

def test_scenario_g_remains_disclosed_false_positive():
    from scenarios import scenario_g
    results = scenario_g.run()
    assert len(results) == 1
    result = results[0]
    assert result.decisions == ["APPROVE"]
    assert result.allowed is False and result.passed is False
    assert any("conservative" in d for d in result.details)


def test_run_all_report_keeps_g_disclosed_and_counts_stable():
    from scenarios import run_all
    metrics = run_all.collect()
    # Unauthorized interception is untouched by the Option A decision:
    assert metrics["unauthorized_total"] == 5
    assert metrics["unauthorized_intercepted"] == 5
    # Conservative taint keeps the disclosed false positive:
    assert metrics["legitimate_allowed"] == 2
    assert metrics["legitimate_total"] == 3
    assert any("G:" in name for name in metrics["false_positives"])
    report = run_all.render_report(metrics)
    assert "2/3" in report and "False positives (disclosed, not hidden)" in report


# --- 8. Auditability of session reads -----------------------------------------

def test_session_read_stays_audited_regardless_of_later_flow():
    from scenarios.harness import TASK, Harness
    with Harness() as harness:
        task_id = harness.client.create_task(TASK)["task_id"]
        harness.client.call_tool(task_id, "read_file",
                                 {"path": "hr/employee_salary.csv"})
        harness.client.call_tool(
            task_id, "send_slack",
            {"channel": "#sales", "text": "unrelated clean message",
             "source_assets": ["sales_report_q3"],
             "transformation": "summary"})
        events = harness.test_client.get(
            f"/audit?task_id={task_id}").json()["events"]
        reads = [e for e in events if e["action"] == "read"
                 and "employee_salary" in e["sources"]]
        assert reads, events
        assert reads[0]["decision"] == "ALLOW"  # allow-but-taint (§3 Option 1)
        # The read is visible even though the later flow was held for review:
        assert any(e["action"] == "send_message" for e in events)
