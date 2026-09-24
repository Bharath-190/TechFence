"""Tests for taskfence/models.py (kit Prompt A1).

Covers: contract immutability, deterministic version hashing, vocabulary
validation, and frozen Decision/FlowRequest.
"""

import pytest
from pydantic import ValidationError

from taskfence.models import Decision, FlowRequest, TaskContract

NOW = "2026-09-30T14:00:00Z"


def make_contract(**overrides) -> TaskContract:
    fields = dict(
        contract_id="c-1",
        purpose="sales_reporting",
        allowed_data=["sales_reports"],
        allowed_destinations=["sales_slack"],
        allowed_actions=["read", "send_message"],
        external_transfer=False,
        created_at=NOW,
    )
    fields.update(overrides)
    return TaskContract(**fields)


def test_contract_is_frozen():
    contract = make_contract()
    with pytest.raises(ValidationError):
        contract.purpose = "hr_analytics"


def test_same_fields_same_version():
    assert make_contract().version == make_contract().version


@pytest.mark.parametrize("field,value", [
    ("contract_id", "c-2"),
    ("parent_contract_id", "c-0"),
    ("purpose", "hr_analytics"),
    ("allowed_data", ["customer_db"]),
    ("allowed_destinations", ["hr_portal"]),
    ("allowed_actions", ["query"]),
    ("external_transfer", True),
    ("created_at", "2026-09-30T15:00:00Z"),
])
def test_any_field_change_changes_version(field, value):
    assert make_contract(**{field: value}).version != make_contract().version


def test_version_independent_of_list_order():
    a = make_contract(allowed_data=["sales_reports", "public_info"])
    b = make_contract(allowed_data=["public_info", "sales_reports"])
    assert a.version == b.version


def test_unknown_purpose_rejected():
    with pytest.raises(ValidationError):
        make_contract(purpose="world_domination")


def test_unknown_data_group_rejected():
    with pytest.raises(ValidationError):
        make_contract(allowed_data=["crypto_wallet"])


def test_unknown_destination_rejected():
    with pytest.raises(ValidationError):
        make_contract(allowed_destinations=["pastebin"])


def test_unknown_action_rejected():
    with pytest.raises(ValidationError):
        make_contract(allowed_actions=["format_disk"])


def test_unknown_label_rejected_on_asset():
    from taskfence.models import DataAsset
    with pytest.raises(ValidationError):
        DataAsset(id="a1", source="drive", classification="TOP_SECRET")


def test_flow_request_is_frozen():
    request = FlowRequest(tool="send_slack", action="send_message",
                          source="s", source_group="sales_reports",
                          destination="sales_slack")
    with pytest.raises(ValidationError):
        request.destination = "external_api"


def test_decision_is_frozen():
    decision = Decision(outcome="ALLOW", reasons=[], failed_checks=[],
                        contract_version="abc")
    with pytest.raises(ValidationError):
        decision.outcome = "BLOCK"


def test_decision_outcome_is_limited_to_spec_values():
    with pytest.raises(ValidationError):
        Decision(outcome="MAYBE", reasons=[], failed_checks=[],
                 contract_version="abc")
