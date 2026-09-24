"""Table-driven policy engine tests (kit Prompt A2).

Every expected outcome is HARDCODED — none is computed with the code under
test (kit guardrail 5). Baseline is spec §23 Scenario A (ALLOW); the rest
mutate one dimension at a time.
"""

import pytest

from taskfence.models import FlowRequest, TaskContract
from taskfence.policy import PolicyEngine

NOW = "2026-09-30T14:00:00Z"

BASE_CONTRACT = TaskContract(
    contract_id="c-sales",
    purpose="sales_reporting",
    allowed_data=["sales_reports"],
    allowed_destinations=["sales_slack"],
    allowed_actions=["read", "summarize", "send_message"],
    external_transfer=False,
    created_at=NOW,
)

EXT_FORBIDDEN_CONTRACT = TaskContract(
    contract_id="c-sales-ext",
    purpose="sales_reporting",
    allowed_data=["sales_reports"],
    allowed_destinations=["sales_slack", "external_api"],
    allowed_actions=["read", "summarize", "send_message"],
    external_transfer=False,
    created_at=NOW,
)

EXT_ALLOWED_CONTRACT = TaskContract(
    contract_id="c-sales-ext-ok",
    purpose="sales_reporting",
    allowed_data=["sales_reports"],
    allowed_destinations=["sales_slack", "external_api"],
    allowed_actions=["read", "summarize", "send_message"],
    external_transfer=True,
    created_at=NOW,
)

EMPTY_CONTRACT = TaskContract(
    contract_id="c-empty",
    purpose="sales_reporting",
    created_at=NOW,
)


def make_request(**overrides) -> FlowRequest:
    fields = dict(
        tool="send_slack",
        action="send_message",
        source="sales_report_q3.csv",
        source_group="sales_reports",
        destination="sales_slack",
        labels=frozenset({"CONFIDENTIAL"}),
        transformation="summary",
        payload="Q3 sales summary",
    )
    fields.update(overrides)
    return FlowRequest(**fields)


# (name, request overrides, contract, hardcoded expected outcome,
#  hardcoded expected failed_checks or None to skip that assertion)
CASES = [
    ("scenario_a_allow", {}, BASE_CONTRACT, "ALLOW", []),
    ("scenario_b_customer_db_to_external", dict(
        tool="post_external", action="post_external",
        source="customer_db.json", source_group="customer_db",
        destination="external_api",
        labels=frozenset({"CUSTOMER_DATA", "PII"}),
        payload="customer records"),
     BASE_CONTRACT, "BLOCK", None),
    ("scenario_c_salary_derived_to_external", dict(
        tool="post_external", action="post_external",
        source="salary_summary", source_group="employee_salary",
        destination="external_api",
        labels=frozenset({"EMPLOYEE_DATA", "FINANCIAL"}),
        payload="average salary = 82000"),
     BASE_CONTRACT, "BLOCK", None),
    ("c_variant_salary_summary_to_allowed_slack", dict(
        tool="send_slack", action="send_message",
        source="salary_summary", source_group="employee_salary",
        destination="sales_slack",
        labels=frozenset({"EMPLOYEE_DATA", "FINANCIAL"})),
     BASE_CONTRACT, "APPROVE",
     ["restricted_label_review", "source_outside_contract"]),
    ("customer_data_to_allowed_internal_dest", dict(
        source="customer_db.json", source_group="customer_db",
        labels=frozenset({"CUSTOMER_DATA"})),
     BASE_CONTRACT, "APPROVE", None),
    ("out_of_scope_source_group", dict(
        source="strategy.txt", source_group="internal_strategy"),
     BASE_CONTRACT, "APPROVE", ["source_outside_contract"]),
    ("unknown_source_group", dict(
        source="mystery.bin", source_group="crypto_wallet"),
     BASE_CONTRACT, "BLOCK", None),
    ("unknown_destination", dict(
        source="sales_report_q3.csv",
        destination="pastebin", labels=frozenset()),
     BASE_CONTRACT, "BLOCK", None),
    ("unknown_action", dict(action="email_everyone"),
     BASE_CONTRACT, "BLOCK", None),
    ("unknown_tool", dict(tool="curl"),
     BASE_CONTRACT, "BLOCK", None),
    ("action_out_of_scope_soft", dict(action="derive"),
     BASE_CONTRACT, "APPROVE", ["action_outside_contract"]),
    ("external_dest_allowed_but_transfer_disabled", dict(
        destination="external_api", labels=frozenset()),
     EXT_FORBIDDEN_CONTRACT, "BLOCK", None),
    ("external_dest_allowed_and_transfer_enabled", dict(
        destination="external_api", labels=frozenset()),
     EXT_ALLOWED_CONTRACT, "ALLOW", []),
    ("empty_contract_never_allows", {}, EMPTY_CONTRACT, "BLOCK", None),
    ("multiple_failures_collected", dict(
        source="strategy.txt", source_group="internal_strategy",
        tool="post_external", action="post_external",
        destination="external_api", labels=frozenset()),
     BASE_CONTRACT, "BLOCK", None),
]


@pytest.mark.parametrize(
    "name,overrides,contract,expected_outcome,expected_failed",
    CASES, ids=[c[0] for c in CASES])
def test_policy_table(name, overrides, contract, expected_outcome,
                      expected_failed):
    decision = PolicyEngine().evaluate(contract, make_request(**overrides))
    assert decision.outcome == expected_outcome, decision.reasons
    if expected_failed is not None:
        assert decision.failed_checks == expected_failed
    assert decision.contract_version == contract.version


def test_scenario_b_reasons_are_complete_and_plain_language():
    decision = PolicyEngine().evaluate(
        BASE_CONTRACT,
        make_request(tool="post_external", action="post_external",
                     source="customer_db.json", source_group="customer_db",
                     destination="external_api",
                     labels=frozenset({"CUSTOMER_DATA", "PII"})))
    assert decision.outcome == "BLOCK"
    joined = " ".join(decision.reasons)
    assert any("customer_db" in r for r in decision.reasons), joined
    assert any("CUSTOMER_DATA" in r or "PII" in r for r in decision.reasons)
    assert any("external_api" in r for r in decision.reasons)
    for check in ("external_destination_forbidden", "restricted_label_escape"):
        assert check in decision.failed_checks


def test_scenario_c_blocks_via_inherited_labels_not_keywords():
    payload = "quarterly aggregate: 42"  # no salary keywords at all
    decision = PolicyEngine().evaluate(
        BASE_CONTRACT,
        make_request(tool="post_external", action="post_external",
                     source="salary_summary", source_group="employee_salary",
                     destination="external_api",
                     labels=frozenset({"EMPLOYEE_DATA", "FINANCIAL"}),
                     payload=payload))
    assert decision.outcome == "BLOCK"
    assert any("EMPLOYEE_DATA" in r or "FINANCIAL" in r
               for r in decision.reasons)


def test_same_input_100x_identical_decision():
    engine = PolicyEngine()
    request = make_request(tool="post_external", action="post_external",
                           source="customer_db.json",
                           source_group="customer_db",
                           destination="external_api",
                           labels=frozenset({"CUSTOMER_DATA"}))
    first = engine.evaluate(BASE_CONTRACT, request)
    for _ in range(100):
        assert engine.evaluate(BASE_CONTRACT, request) == first
