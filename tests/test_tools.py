"""Tests for taskfence/tools.py (kit Prompt C1): sinks get exactly one JSONL
line per call, tool metadata matches the catalog."""

import pytest

from taskfence import registry, tools

CTX = {"task_id": "t-tools", "source_asset": "sales_report_q3",
       "content": "Q3 summary text"}


@pytest.fixture(autouse=True)
def _clean_outbox():
    registry.reset_outbox()
    yield
    registry.reset_outbox()


def test_read_file_returns_fake_report():
    result = tools.read_file(CTX, path="sales_report_q3.csv")
    assert result["asset_id"] == "sales_report_q3"
    assert "North,Anvil Pro" in result["content"]


def test_query_customer_db_filters_by_region():
    result = tools.query_customer_db(
        {"task_id": "t", "source_asset": None, "content": ""},
        query="North")
    assert result["count"] == 6
    assert all("North" in r["region"] for r in result["results"])


def test_query_customer_db_empty_query_returns_all():
    result = tools.query_customer_db(
        {"task_id": "t", "source_asset": None, "content": ""})
    assert result["count"] == 30


def test_send_slack_appends_one_line():
    result = tools.send_slack(CTX, channel="#sales")
    assert result["delivered"] is True
    lines = registry.sink_lines("slack_sales")
    assert len(lines) == 1
    assert lines[0]["channel"] == "#sales"
    assert lines[0]["text"] == "Q3 summary text"
    assert lines[0]["task_id"] == "t-tools"


def test_post_external_appends_one_line():
    result = tools.post_external(CTX, url="https://collect.example.invalid/x")
    assert result["sent"] is True
    lines = registry.sink_lines("external_api")
    assert len(lines) == 1
    assert lines[0]["url"] == "https://collect.example.invalid/x"
    assert lines[0]["payload"] == "Q3 summary text"


def test_each_call_exactly_one_jsonl_line():
    tools.send_slack(CTX)
    tools.send_slack(CTX)
    assert len(registry.sink_lines("slack_sales")) == 2
    tools.post_external(CTX)
    assert len(registry.sink_lines("external_api")) == 1


def test_tool_metadata_flags():
    assert tools.TOOL_REGISTRY["post_external"]["is_external"] is True
    assert tools.TOOL_REGISTRY["send_slack"]["is_external"] is False
    assert tools.TOOL_REGISTRY["send_slack"]["is_outbound"] is True
    assert tools.TOOL_REGISTRY["read_file"]["is_outbound"] is False
    assert tools.TOOL_REGISTRY["read_file"]["action"] == "read"


def test_every_registered_tool_has_a_function():
    assert set(tools.TOOL_FUNCTIONS) == set(tools.TOOL_REGISTRY)
    assert set(tools.TOOL_FUNCTIONS) <= {"read_file", "query_customer_db",
                                         "send_slack", "post_external"}
