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


def _demo_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return tuple(conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("audit_events", "task_state", "approvals"))
    finally:
        conn.close()


def test_pending_approval_card_shows_timestamp_and_age(seeded_db):
    """R2: the human sees when a request was created and how long it has
    been pending. Informational only — nothing expires."""
    from datetime import datetime, timedelta, timezone
    from streamlit.testing.v1 import AppTest

    created_at = (datetime.now(timezone.utc)
                  - timedelta(days=5, hours=1)).isoformat(timespec="seconds")
    conn = sqlite3.connect(seeded_db)
    conn.execute(
        "INSERT INTO approvals (approval_id, task_id, tool, args, reasons,"
        " status, created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("apr-age", "t1", "send_slack", "{}", "needs review", "pending",
         created_at, None))
    conn.commit()
    conn.close()

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    rendered = "\n".join(md.value or "" for md in at.markdown)
    assert "Pending since" in rendered
    assert created_at in rendered
    assert "5 days ago" in rendered


def test_demo_reset_requires_explicit_confirmation(seeded_db):
    """F3: the destructive reset is an operator action. Clicking it without
    the confirmation checkbox deletes nothing and says so; the checkbox
    label names everything that would be removed."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception

    reset_buttons = [b for b in at.button if "Reset" in b.label]
    assert len(reset_buttons) == 1, [b.label for b in at.button]
    confirmations = [c for c in at.checkbox
                     if "permanently deletes" in c.label]
    assert len(confirmations) == 1
    assert "decisions" in confirmations[0].label  # effect is stated plainly
    assert confirmations[0].value is False         # opt-in, never preselected

    before = _demo_counts(seeded_db)
    reset_buttons[0].click().run()
    assert not at.exception, at.exception
    assert _demo_counts(seeded_db) == before  # nothing deleted
    assert any("NOT performed" in w.value for w in at.warning)


def test_confirmed_demo_reset_clears_only_demo_rows(seeded_db, tmp_path,
                                                   monkeypatch):
    """The confirmed branch still works: with the box ticked, the demo
    rows are cleared. Runs in a temporary CWD so the repository's tracked
    outbox sinks are never touched."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    at.checkbox[0].check().run()
    reset = [b for b in at.button if "Reset" in b.label]
    assert len(reset) == 1
    reset[0].click().run()
    assert not at.exception, at.exception
    assert _demo_counts(seeded_db) == (0, 0, 0)
    # The repository's own outbox sinks are untouched:
    assert not (tmp_path / "outbox").exists()


def _enable_jwt_mode(monkeypatch) -> None:
    """Point the dashboard process at a valid JWT configuration."""
    from tests.jwt_keys import TEST_PUBLIC_KEY

    monkeypatch.setenv("TASKFENCE_AUTH_MODE", "jwt")
    monkeypatch.setenv("TASKFENCE_JWT_PUBLIC_KEY", TEST_PUBLIC_KEY)
    monkeypatch.setenv("TASKFENCE_JWT_ISSUER", "https://issuer.test/")
    monkeypatch.setenv("TASKFENCE_JWT_AUDIENCE", "taskfence-api")


def test_jwt_mode_without_a_reviewer_token_is_called_out(seeded_db,
                                                        monkeypatch):
    """Phase 3 (G-04): the dashboard must not leave the operator guessing
    why approval calls fail. JWT mode without a reviewer token gets an
    explicit message naming the variable to set."""
    from streamlit.testing.v1 import AppTest

    _enable_jwt_mode(monkeypatch)
    monkeypatch.delenv("TASKFENCE_REVIEWER_JWT", raising=False)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    errors = " ".join(error.value or "" for error in at.error)
    assert "TASKFENCE_REVIEWER_JWT" in errors


def test_jwt_mode_with_a_reviewer_token_does_not_warn(seeded_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    _enable_jwt_mode(monkeypatch)
    monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", "test-access-token")

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    errors = " ".join(error.value or "" for error in at.error)
    assert "TASKFENCE_REVIEWER_JWT" not in errors


def test_local_mode_does_not_mention_a_reviewer_token(seeded_db, monkeypatch):
    """The JWT guidance appears only when JWT mode is actually configured."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("TASKFENCE_AUTH_MODE", raising=False)
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    errors = " ".join(error.value or "" for error in at.error)
    assert "TASKFENCE_REVIEWER_JWT" not in errors


def test_dashboard_empty_db_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKFENCE_DB", str(tmp_path / "missing.sqlite3"))
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception
