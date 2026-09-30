"""Dashboard decision-panel tests (kit I6) via streamlit.testing.v1.AppTest.

The panel must render every field verbatim from stored task state and the
audit table — the UI computes nothing about security. The seeded DB here is
the same layout the existing smoke tests use (fixture imported from
tests/test_dashboard_smoke.py, so no committed test files are touched).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_smoke import seeded_db, APP_PATH  # noqa: E402,F401

# The state blob seeded by seeded_db pins the stored last_decision.
# Note: the committed fixture stores the explain with literal backslash-n
# sequences; the panel renders stored values verbatim, so expect those:
BLOCK_EXPLAIN = ("BLOCKED\\n\\nWhy:\\n- Destination 'external_api' "
                 "is external and not allowed by the task.")
CONTRACT_PURPOSE = "sales_reporting"
CONTRACT_VERSION_PREFIX = "a" * 12
STORED_LINEAGE = "customer_db -> external_api"


def _run_app():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def _panel_text(at) -> str:
    """All markdown/code text rendered by the app (decision panel lives in
    markdown blocks; the Why? explain in a code block)."""
    markdowns = " ".join(md.value or "" for md in at.markdown)
    codes = " ".join(code.value or "" for code in at.code)
    return markdowns + "\n" + codes


def test_panel_renders_all_six_fields_from_stored_state(seeded_db):
    at = _run_app()
    assert not at.exception, at.exception

    # The five required sections exist, clearly separated:
    headers = [subheader.value for subheader in at.subheader]
    # Phase 7 (demo clarity): the header now reads CURRENT SECURITY
    # DECISION to distinguish the intermediate decision from the FINAL
    # SCENARIO OUTCOME rendered by the SECURITY FLOW panel.
    assert "CURRENT SECURITY DECISION" in headers
    assert "AUDIT TRAIL" in headers
    assert "CURRENT TASK" in headers
    assert "CONTRACT SCOPE" in headers

    text = _panel_text(at)
    # 1. Decision — the colored outcome, verbatim from stored state:
    assert "DECISION:" in text and "BLOCK" in text
    # 2. Why? — the stored explain.py output, verbatim:
    assert BLOCK_EXPLAIN in text
    # 3. Data Flow — source -> transformation -> destination from the
    #    stored audit event (customer_db, export, external_api):
    assert "customer_db -> export -> external_api" in text
    # 4. Contract — purpose + version short form, from stored contract:
    assert CONTRACT_PURPOSE in text
    assert f"v{CONTRACT_VERSION_PREFIX}" in text
    # 5. Classification — effective labels verbatim from the audit event:
    assert "CUSTOMER_DATA,PII" in text
    # 6. Lineage — the stored lineage path verbatim:
    assert STORED_LINEAGE in text


def test_panel_is_absent_without_state_and_db_stays_graceful(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("TASKFENCE_DB", str(tmp_path / "missing.sqlite3"))
    at = _run_app()
    assert not at.exception, at.exception
    headers = [subheader.value for subheader in at.subheader]
    assert "CURRENT SECURITY DECISION" in headers  # section always present
    assert "No decision yet" in " ".join(
        info.value for info in at.info)           # graceful empty state


def test_audit_trail_has_approval_id_column(seeded_db):
    at = _run_app()
    assert not at.exception, at.exception
    table_text = at.dataframe[0].value.to_string()
    # The approval_id column exists (kit I6); empty here — no approvals in
    # the seeded DB — but the column must be present:
    assert "approval_id" in at.dataframe[0].value.columns
    assert "approval_id" in table_text or "approval_id" in str(
        at.dataframe[0].value.columns)
