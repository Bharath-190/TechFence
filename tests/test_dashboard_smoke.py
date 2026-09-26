"""Dashboard smoke test (kit Prompt F-T) via streamlit.testing.v1.AppTest.

Seeds a temporary SQLite DB with one ALLOW and one BLOCK audit event and a
task-state row, then asserts the app runs without exception, the audit
table shows both decisions, and the BLOCK reason text is rendered.
Does NOT change source files; a failure means a dashboard bug to report.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

APP_PATH = Path("dashboard/app.py").resolve()


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "dash.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT, task_id TEXT, task_text TEXT, contract_id TEXT,"
        " contract_version TEXT, tool TEXT, action TEXT, sources TEXT,"
        " labels TEXT, transformation TEXT, destination TEXT,"
        " decision TEXT, reasons TEXT, lineage_path TEXT);"
        "CREATE TABLE task_state (task_id TEXT PRIMARY KEY,"
        " updated_at TEXT, state TEXT);"
        "CREATE TABLE approvals (approval_id TEXT PRIMARY KEY,"
        " task_id TEXT, tool TEXT, args TEXT, reasons TEXT,"
        " status TEXT, created_at TEXT, resolved_at TEXT);")
    contract = {
        "contract_id": "c-1", "parent_contract_id": None,
        "purpose": "sales_reporting", "allowed_data": ["sales_reports"],
        "allowed_destinations": ["sales_slack"],
        "allowed_actions": ["read", "send_message"],
        "external_transfer": False,
        "created_at": "2026-09-30T14:00:00+00:00",
        "version": "a" * 64}
    state = {
        "task_text": "Summarize Q3 sales and post it to #sales.",
        "contract": contract,
        "last_decision": {
            "decision": {"outcome": "BLOCK", "reasons": ["Outside task scope"],
                         "failed_checks": ["external_destination_forbidden"],
                         "contract_version": "a" * 64},
            "explain": "BLOCKED\\n\\nWhy:\\n- Destination 'external_api' "
                       "is external and not allowed by the task.",
            "lineage_path": ["customer_db", "external_api"],
            "tool": "post_external", "destination": "external_api"},
        "session_reads": ["customer_db"], "pending_approvals": []}
    conn.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('2026-09-30T14:00:00+00:00', 't1', 'Q3 task', 'c-1', ?,"
        " 'send_slack', 'send_message', 'sales_report_q3', 'CONFIDENTIAL',"
        " 'summary', 'sales_slack', 'ALLOW', 'Within task scope',"
        " 'sales_report_q3 -> sales_slack')", ("a" * 64,))
    conn.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('2026-09-30T14:01:00+00:00', 't1', 'Q3 task', 'c-1', ?,"
        " 'post_external', 'post_external', 'customer_db',"
        " 'CUSTOMER_DATA,PII', 'export', 'external_api', 'BLOCK',"
        " 'Destination ''external_api'' is external and not allowed by the"
        " task.', 'customer_db -> external_api')", ("a" * 64,))
    conn.execute("INSERT INTO task_state VALUES ('t1', ?, ?)",
                 ("2026-09-30T14:01:00+00:00", json.dumps(state)))
    conn.commit()
    conn.close()
    monkeypatch.setenv("TASKFENCE_DB", str(db))
    return db


def test_dashboard_runs_and_renders_decisions(seeded_db, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(".").resolve()))
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception

    # Audit table shows both decisions (filter defaults to All):
    table_text = at.dataframe[0].value.to_string()
    assert "ALLOW" in table_text and "BLOCK" in table_text

    # The BLOCK explain text is rendered:
    codes = " ".join(code.value or "" for code in at.code)
    assert "external and not allowed" in codes
    assert "BLOCKED" in codes


def test_dashboard_decision_filter(seeded_db):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    at.selectbox[0].set_value("BLOCK").run()
    assert not at.exception
    table_text = at.dataframe[0].value.to_string()
    assert "BLOCK" in table_text
    assert "ALLOW" not in table_text


def test_dashboard_empty_db_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFENCE_DB", str(tmp_path / "missing.sqlite3"))
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception
