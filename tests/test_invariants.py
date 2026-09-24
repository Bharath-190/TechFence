"""Invariant tests (kit Prompt A-T): I2 and I4 for the security core.

This file must not change any source file; a failure means a real bug to be
reported, not silently fixed.
"""

import ast
from pathlib import Path

import pytest

from taskfence.models import FlowRequest, TaskContract
from taskfence.policy import PolicyEngine

NOW = "2026-09-30T14:00:00Z"

# Invariant I2: policy.py may import only these modules.
POLICY_ALLOWED_IMPORTS = {"enum", "typing", "taskfence", "taskfence.catalog",
                          "taskfence.models"}
POLICY_FORBIDDEN_IMPORTS = {"ollama", "requests", "httpx", "fastapi",
                            "sqlite3", "os", "time", "datetime", "random",
                            "socket", "subprocess", "pathlib"}


def _module_imports(path: str) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_policy_imports_respect_invariant_i2():
    imports = _module_imports("taskfence/policy.py")
    assert imports <= POLICY_ALLOWED_IMPORTS, imports
    assert not imports & POLICY_FORBIDDEN_IMPORTS, imports


BASE_CONTRACT = TaskContract(
    contract_id="c-sales",
    purpose="sales_reporting",
    allowed_data=["sales_reports"],
    allowed_destinations=["sales_slack"],
    allowed_actions=["read", "summarize", "send_message"],
    external_transfer=False,
    created_at=NOW,
)

EMPTY_CONTRACT = TaskContract(
    contract_id="c-empty", purpose="sales_reporting", created_at=NOW)


def make_request(**overrides) -> FlowRequest:
    fields = dict(
        tool="send_slack", action="send_message",
        source="sales_report_q3.csv", source_group="sales_reports",
        destination="sales_slack", labels=frozenset({"CONFIDENTIAL"}),
        transformation="summary", payload="Q3 sales summary")
    fields.update(overrides)
    return FlowRequest(**fields)


# Start from the Scenario A ALLOW request; mutate ONE field at a time.
# Hardcoded expected outcomes — none may be ALLOW (kit Prompt A-T).
MUTATIONS = [
    ("source_outside_allowed_data", dict(source="strategy.txt",
                                         source_group="internal_strategy"),
     "APPROVE"),
    ("source_unresolvable", dict(source="mystery.bin",
                                 source_group=""), "BLOCK"),
    ("action_outbound_out_of_scope", dict(tool="post_external",
                                          action="post_external"), "BLOCK"),
    ("action_out_of_scope_soft", dict(action="derive"), "APPROVE"),
    ("destination_not_allowed", dict(destination="internal_wiki"), "BLOCK"),
    ("external_destination", dict(destination="external_api"), "BLOCK"),
    ("restricted_label_in_lineage", dict(
        labels=frozenset({"CONFIDENTIAL", "SOURCE_CODE"})), "APPROVE"),
    ("restricted_label_external", dict(
        destination="external_api",
        labels=frozenset({"CONFIDENTIAL", "SOURCE_CODE"})), "BLOCK"),
    ("unknown_tool", dict(tool="curl"), "BLOCK"),
    ("unknown_destination", dict(destination="pastebin"), "BLOCK"),
    ("unknown_action", dict(action="email_everyone"), "BLOCK"),
]


@pytest.mark.parametrize("name,overrides,expected", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_single_field_mutation_never_allows(name, overrides, expected):
    decision = PolicyEngine().evaluate(BASE_CONTRACT, make_request(**overrides))
    assert decision.outcome == expected, decision.reasons
    assert decision.outcome != "ALLOW"


def test_base_request_is_allow():
    decision = PolicyEngine().evaluate(BASE_CONTRACT, make_request())
    assert decision.outcome == "ALLOW"


def test_empty_contract_never_allows():
    decision = PolicyEngine().evaluate(EMPTY_CONTRACT, make_request())
    assert decision.outcome != "ALLOW"
