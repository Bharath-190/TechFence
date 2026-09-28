"""Metrics-sourcing tests (kit I7).

Every number the dashboard or README displays must trace to a real
computed value. These tests prove it the kit's way: change the underlying
data and assert the displayed figure changes — a static string would fail
this. No security logic is exercised; the metrics are pure display
sourcing (report parse + audit-row counts).
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_smoke import APP_PATH  # noqa: E402

REPORT = Path("reports/results.md")


@pytest.fixture()
def audit_db(tmp_path, monkeypatch):
    """A stored session with known audit rows + one task-state row."""
    db = tmp_path / "metrics.sqlite3"
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
    rows = [
        ("t1", "read_file", "read", "ALLOW"),
        ("t1", "send_slack", "send_message", "ALLOW"),
        ("t2", "send_slack", "send_message", "APPROVE"),        # gated
        ("t2", "send_slack", "approval_resolve", "ALLOW"),      # one-time
        ("t3", "post_external", "post_external", "BLOCK"),
    ]
    for task_id, tool, action, decision in rows:
        conn.execute(
            "INSERT INTO audit_events (timestamp, task_id, task_text,"
            " contract_id, contract_version, tool, action, sources, labels,"
            " transformation, destination, decision, reasons, lineage_path)"
            " VALUES ('2026-09-28T10:00:00+00:00', ?, 'task', 'c-1', 'v',"
            " ?, ?, 'src', 'INTERNAL', 'none', 'dst', ?,"
            " 'a reason', 'src -> dst')", (task_id, tool, action, decision))
    conn.execute("INSERT INTO task_state VALUES ('t1', 'x', '{}')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("TASKFENCE_DB", str(db))
    return db


def _run_app():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    return at


def _metrics(at) -> dict:
    values = [m.value for m in at.metric]
    names = [m.label for m in at.metric]
    return dict(zip(names, values))


def test_report_sourced_metrics_match_generated_report(audit_db):
    """The X/Y figures equal reports/results.md's own numbers — and the
    scenario count equals the report's table row count, never a constant."""
    from dashboard.app import _scenario_report_metrics
    metrics = _scenario_report_metrics()
    text = REPORT.read_text(encoding="utf-8")
    assert metrics["unauthorized"] == "5/5"
    assert metrics["legitimate"] == "2/3"
    # "Scenarios reproduced" counts reproduced FLOWS (the report's table
    # rows): C yields C+C2, D yields D+D2 — 9 flows from 7 scenario modules:
    assert metrics["scenarios"] == 9
    assert text.count("| PASS |") + text.count("| FAIL |") == 9


def test_changing_report_changes_displayed_figures(audit_db, monkeypatch,
                                                   tmp_path):
    """Kit's sourcing proof: mutate the underlying data (a doctored report)
    and the displayed numbers must follow — no hardcoded string passes."""
    doctored = tmp_path / "doctored.md"
    doctored.write_text(
        "# TaskFence — Scenario Results\n\n"
        "| Scenario | Kind | Expectation | Observed | Verdict |\n"
        "|---|---|---|---|---|\n"
        "| A | legitimate | x | y | PASS |\n"
        "| B | unauthorized | x | y | PASS |\n"
        "\nUnauthorized flows intercepted: **7/7**\n"
        "Legitimate flows allowed: **1/1**\n",
        encoding="utf-8")
    monkeypatch.setenv("TASKFENCE_REPORT_PATH", str(doctored))
    at = _run_app()
    assert not at.exception, at.exception
    metrics = _metrics(at)
    assert metrics["Unauthorized flows intercepted"] == "7/7"
    assert metrics["Legitimate flows allowed"] == "1/1"
    # AppTest renders metric numbers as strings; the VALUE followed the data:
    assert metrics["Scenarios reproduced"] in (2, "2")  # 3 rows − header


def test_audit_metrics_count_approvals_and_one_time(audit_db):
    """Approval-gated / one-time counts derive from stored audit rows
    (APPROVE decisions; approval_resolve ALLOW events) — not tallies."""
    from dashboard.app import _audit_metrics
    metrics = _audit_metrics()
    assert metrics["approval_gated"] == 1   # the single APPROVE decision
    assert metrics["one_time"] == 1         # the approval_resolve ALLOW
    assert metrics["events"] == 5
    assert metrics["incomplete"] == 0


def test_changing_audit_rows_changes_displayed_counts(audit_db):
    """Add another APPROVE + resolve pair → both displayed counts move."""
    conn = sqlite3.connect(audit_db)
    conn.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('2026-09-28T10:01:00+00:00', 't4', 'task', 'c-1', 'v',"
        " 'send_slack', 'send_message', 'src', 'INTERNAL', 'none', 'dst',"
        " 'APPROVE', 'a reason', 'src')")
    conn.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('2026-09-28T10:02:00+00:00', 't4', 'task', 'c-1', 'v',"
        " 'send_slack', 'approval_resolve', 'src', 'INTERNAL',"
        " 'allow_once', 'dst', 'ALLOW', 'Human approved this single"
        " request.', 'src -> dst')")
    conn.commit()
    conn.close()
    from dashboard.app import _audit_metrics
    metrics = _audit_metrics()
    assert metrics["approval_gated"] == 2
    assert metrics["one_time"] == 2
    at = _run_app()
    assert not at.exception, at.exception
    rendered = _metrics(at)
    assert int(rendered["Approval-gated flows"]) == 2
    assert int(rendered["One-time approvals executed"]) == 2


def test_audit_coverage_is_structural_and_never_a_bare_100_percent(
        audit_db):
    """The kit's honesty rule: no unverifiable '100%'. Coverage is shown
    as a verified event count with an explicit scope caveat, and degrades
    to 'incomplete' when the data stops supporting it."""
    at = _run_app()
    assert not at.exception, at.exception
    metrics = _metrics(at)
    coverage = metrics["Audit coverage"]
    assert coverage == "5/5 events"
    assert "100%" not in str(coverage)

    # Break the data → the check must visibly degrade, not stay rosy.
    # Realistic failure: the gateway persists task state on POST /tasks
    # BEFORE any tool call — a task with no audit events yet:
    conn = sqlite3.connect(audit_db)
    conn.execute("INSERT INTO task_state VALUES ('t9', 'x', '{}')")
    conn.commit()
    conn.close()
    at2 = _run_app()
    assert not at2.exception, at2.exception
    assert _metrics(at2)["Audit coverage"] == "incomplete"


def test_readme_metrics_match_generated_report():
    """README's numbers are the generated report's numbers — enforced, so
    a stale README fails here instead of reaching a judge."""
    readme = Path("README.md").read_text(encoding="utf-8")
    report = REPORT.read_text(encoding="utf-8")
    assert "**5/5**" in readme and "**5/5**" in report
    assert "**2/3**" in readme and "**2/3**" in report
    assert "9 scenarios reproduced" in readme
    # 9 = reproduced flows (report rows), not module count (7 — C→C+C2,
    # D→D+D2). Cross-check the report's own row count:
    from scenarios.run_all import ALL_SCENARIOS
    assert len(ALL_SCENARIOS) == 7
    assert report.count("| PASS |") + report.count("| FAIL |") == 9
