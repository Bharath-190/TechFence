"""Snapshot tests for taskfence/explain.py (kit Prompt A3).

Expected strings are hardcoded; the decisions are produced by the real policy
engine so any drift between reasons and explanations is caught.
"""

from taskfence.models import FlowRequest, TaskContract
from taskfence.explain import explain
from taskfence.policy import PolicyEngine

NOW = "2026-09-30T14:00:00Z"

CONTRACT = TaskContract(
    contract_id="c-sales",
    purpose="sales_reporting",
    allowed_data=["sales_reports"],
    allowed_destinations=["sales_slack"],
    allowed_actions=["read", "summarize", "send_message"],
    external_transfer=False,
    created_at=NOW,
)

ENGINE = PolicyEngine()


def test_scenario_a_allow_snapshot():
    request = FlowRequest(
        tool="send_slack", action="send_message",
        source="sales_report_q3.csv", source_group="sales_reports",
        destination="sales_slack", labels=frozenset({"CONFIDENTIAL"}),
        transformation="summary", payload="Q3 sales summary")
    decision = ENGINE.evaluate(CONTRACT, request)
    text = explain(decision, request, CONTRACT,
                   ["sales_report_q3.csv", "sales_slack"])
    assert text == (
        "ALLOWED\n"
        "\n"
        "Source: sales_report_q3.csv\n"
        "Destination: sales_slack\n"
        "Task: sales_reporting\n"
        "\n"
        "Why:\n"
        "Flow is within task scope.\n"
        "\n"
        "Lineage: sales_report_q3.csv -> sales_slack"
    )


def test_scenario_b_block_snapshot():
    request = FlowRequest(
        tool="post_external", action="post_external",
        source="customer_db.json", source_group="customer_db",
        destination="external_api",
        labels=frozenset({"CUSTOMER_DATA", "PII"}),
        transformation="export", payload="customer records")
    decision = ENGINE.evaluate(CONTRACT, request)
    text = explain(decision, request, CONTRACT,
                   ["customer_db.json", "external_api"])
    assert text == (
        "BLOCKED\n"
        "\n"
        "Source: customer_db.json\n"
        "Destination: external_api\n"
        "Task: sales_reporting\n"
        "\n"
        "Why:\n"
        "- Outbound action 'post_external' is not authorized by the task.\n"
        "- Destination 'external_api' is external and not allowed by the task.\n"
        "- Restricted data (CUSTOMER_DATA, PII) cannot go to 'external_api'.\n"
        "- This movement is not justified by the task purpose.\n"
        "- Data group 'customer_db' is not in the task's allowed data.\n"
        "\n"
        "Lineage: customer_db.json -> external_api"
    )


def test_scenario_c_block_snapshot():
    request = FlowRequest(
        tool="post_external", action="post_external",
        source="salary_summary", source_group="employee_salary",
        destination="external_api",
        labels=frozenset({"EMPLOYEE_DATA", "FINANCIAL"}),
        transformation="average", payload="average salary = 82000")
    decision = ENGINE.evaluate(CONTRACT, request)
    text = explain(decision, request, CONTRACT,
                   ["salary_csv", "salary_summary", "external_api"])
    assert text == (
        "BLOCKED\n"
        "\n"
        "Source: salary_summary\n"
        "Destination: external_api\n"
        "Task: sales_reporting\n"
        "\n"
        "Why:\n"
        "- Outbound action 'post_external' is not authorized by the task.\n"
        "- Destination 'external_api' is external and not allowed by the task.\n"
        "- Restricted data (EMPLOYEE_DATA, FINANCIAL) cannot go to "
        "'external_api'.\n"
        "- This movement is not justified by the task purpose.\n"
        "- Data group 'employee_salary' is not in the task's allowed data.\n"
        "\n"
        "Lineage: salary_csv -> salary_summary -> external_api"
    )


def test_approve_snapshot():
    request = FlowRequest(
        tool="send_slack", action="send_message",
        source="customer_db.json", source_group="customer_db",
        destination="sales_slack", labels=frozenset({"CUSTOMER_DATA"}),
        transformation="summary", payload="regional conversion rates")
    decision = ENGINE.evaluate(CONTRACT, request)
    text = explain(decision, request, CONTRACT,
                   ["customer_db.json", "sales_slack"])
    assert text == (
        "NEEDS APPROVAL\n"
        "\n"
        "Source: customer_db.json\n"
        "Destination: sales_slack\n"
        "Task: sales_reporting\n"
        "\n"
        "Why:\n"
        "- Data group 'customer_db' is not in the task's allowed data.\n"
        "- Restricted data (CUSTOMER_DATA) requires human review before it "
        "moves to 'sales_slack'.\n"
        "\n"
        "This flow may be legitimate but needs human confirmation.\n"
        "Status: waiting for approval.\n"
        "\n"
        "Lineage: customer_db.json -> sales_slack"
    )
