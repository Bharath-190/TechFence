"""Contract builder tests (kit Prompt D1), Ollama monkeypatched."""

import ast
from pathlib import Path

import pytest

from taskfence import contract
from taskfence.catalog import (ACTIONS, DATA_GROUPS, DESTINATIONS, PURPOSES)
from taskfence.models import TaskContract

VALID_JSON = ('{"purpose": "sales_reporting", "allowed_data": '
              '["sales_reports", "crypto_wallet"], "allowed_destinations": '
              '["sales_slack", "pastebin"], "allowed_actions": ["read", '
              '"send_message", "fly"], "external_transfer": false}')


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    monkeypatch.setattr(contract, "_ollama_chat", lambda task_text: None)


def test_valid_llm_json_is_intersected_with_catalog(monkeypatch):
    monkeypatch.setattr(contract, "_ollama_chat", lambda t: VALID_JSON)
    contract_value, path = contract.build_contract_with_path(
        "Summarize Q3 sales and post it to #sales.")
    assert path == "llm"
    assert contract_value.purpose == "sales_reporting"
    # Unknown values dropped, known ones kept:
    assert contract_value.allowed_data == ["sales_reports"]
    assert contract_value.allowed_destinations == ["sales_slack"]
    assert contract_value.allowed_actions == ["read", "send_message"]
    assert contract_value.external_transfer is False


def test_invalid_json_falls_back(monkeypatch):
    monkeypatch.setattr(contract, "_ollama_chat", lambda t: "not json at all")
    contract_value, path = contract.build_contract_with_path(
        "Review the repo deploy notes.")
    assert path == "fallback"
    assert contract_value.purpose == "code_review"


def test_ollama_down_falls_back(monkeypatch):
    def _boom(task_text):
        raise ConnectionError("no ollama here")

    monkeypatch.setattr(contract, "_ollama_chat", _boom)
    contract_value, path = contract.build_contract_with_path(
        "Prepare the Q3 sales report and post it to #sales.")
    assert path == "fallback"
    assert contract_value.purpose == "sales_reporting"
    assert contract_value.allowed_destinations == ["sales_slack"]


def test_fallback_is_deterministic():
    a = contract._fallback_contract("Review the repo deploy notes.")
    b = contract._fallback_contract("Review the repo deploy notes.")
    assert a == b


def test_external_transfer_only_when_task_text_names_it():
    _, without = contract.build_contract_with_path(
        "Post the summary to the external api please.")
    assert without == "fallback"
    assert contract._fallback_contract(
        "Post the summary to the external api please.")["allowed_actions"]


def test_unknown_purpose_falls_back(monkeypatch):
    monkeypatch.setattr(
        contract, "_ollama_chat",
        lambda t: '{"purpose": "world_domination", "allowed_data": []}')
    contract_value, path = contract.build_contract_with_path(
        "Prepare the Q3 sales report.")
    assert path == "fallback"


def test_build_contract_signature_accepts_only_the_task_string():
    """Invariant I7: no document/output/agent-message parameter exists."""
    source = Path("taskfence/contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builder = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "build_contract")
    params = [arg.arg for arg in builder.args.args
              if arg.arg not in ("self",)]
    assert params == ["task_text"]


def test_last_builder_records_path(monkeypatch):
    monkeypatch.setattr(contract, "_ollama_chat", lambda t: None)
    contract.build_contract("Summarize Q3 sales.")
    assert contract.last_builder() == "fallback"
    monkeypatch.setattr(contract, "_ollama_chat", lambda t: VALID_JSON)
    contract.build_contract("Summarize Q3 sales.")
    assert contract.last_builder() == "llm"


def test_fallback_respects_vocabulary():
    fields = contract._fallback_contract(
        "Audit employee salary data and email me the average.")
    assert fields["purpose"] in PURPOSES
    assert set(fields["allowed_data"]) <= DATA_GROUPS
    assert set(fields["allowed_destinations"]) <= DESTINATIONS
    assert set(fields["allowed_actions"]) <= ACTIONS
