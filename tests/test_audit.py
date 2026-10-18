"""Audit tests (kit Prompt B4): append-only, inspectable public surface."""

import inspect
import sqlite3

from taskfence.audit import AuditLog

SAMPLE_EVENT = {
    "task_id": "t-1",
    "task_text": "Prepare Q3 sales report and post to #sales.",
    "contract_id": "c-1",
    "contract_version": "abc123",
    "tool": "post_external",
    "action": "post_external",
    "sources": ["customer_db"],
    "labels": ["CUSTOMER_DATA", "PII"],
    "transformation": "export",
    "destination": "external_api",
    "decision": "BLOCK",
    "reasons": ["Destination 'external_api' is external and not allowed."],
    "lineage_path": "customer_db -> external_api",
}


def test_append_then_read(tmp_path):
    log = AuditLog(db_path=tmp_path / "audit.db")
    row_id = log.log_event(SAMPLE_EVENT)
    rows = log.read_all()
    assert len(rows) == 1 and rows[0]["id"] == row_id
    assert rows[0]["subject"] is None  # reserved identity field stays NULL

    supplied_id = log.log_event(dict(SAMPLE_EVENT, subject="auth0|explicit"))
    supplied = next(row for row in log.read_all()
                    if row["id"] == supplied_id)
    assert supplied["subject"] == "auth0|explicit"  # explicit identity retained
    assert rows[0]["decision"] == "BLOCK"
    assert "CUSTOMER_DATA" in rows[0]["labels"]
    assert "customer_db" in rows[0]["sources"]
    assert "external_api" in rows[0]["lineage_path"]
    assert rows[0]["timestamp"]  # wall-clock allowed here, only here
    log.close()


def test_read_filter_by_task(tmp_path):
    log = AuditLog(db_path=tmp_path / "audit.db")
    log.log_event(SAMPLE_EVENT)
    other = dict(SAMPLE_EVENT, task_id="t-2")
    log.log_event(other)
    assert log.count() == 2
    assert log.count("t-2") == 1
    assert all(r["task_id"] == "t-2" for r in log.read_all("t-2"))
    log.close()


def test_module_has_no_update_or_delete_functions():
    public = [name for name, member in inspect.getmembers(AuditLog)
              if not name.startswith("_")]
    banned = [name for name in public
              if "update" in name.lower() or "delete" in name.lower()]
    assert banned == [], banned


def test_subject_column_is_added_to_a_pre_phase_3_database(tmp_path):
    """Forward-only migration (AD-03): a database created before the identity
    column existed gains it, existing rows keep their history, and the
    reviewer subject is written to the dedicated column — not to `reasons`.

    The same `_ensure_column` path runs on PostgreSQL (via
    information_schema); this proves the SQLite half that can run here.
    """
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT NOT NULL, task_id TEXT NOT NULL,"
        " task_text TEXT NOT NULL, contract_id TEXT NOT NULL,"
        " contract_version TEXT NOT NULL, tool TEXT NOT NULL,"
        " action TEXT NOT NULL, sources TEXT NOT NULL, labels TEXT NOT NULL,"
        " transformation TEXT NOT NULL, destination TEXT NOT NULL,"
        " decision TEXT NOT NULL, reasons TEXT NOT NULL,"
        " lineage_path TEXT NOT NULL);")
    legacy.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('2026-01-01T00:00:00+00:00', 'task-legacy', 'old task', '',"
        " '', 'read_file', 'read', '', '', '', '', 'ALLOW', 'legacy row', '')")
    legacy.commit()
    legacy.close()

    log = AuditLog(db_path=db_path)
    log.log_event(dict(SAMPLE_EVENT, subject="auth0|upgraded"))
    rows = log.read_all()

    assert rows[0]["subject"] is None          # old row: column added as NULL
    assert rows[-1]["subject"] == "auth0|upgraded"
    assert "auth0|upgraded" not in rows[-1]["reasons"]
    log.close()


def test_log_event_method_cannot_rewrite_history(tmp_path):
    log = AuditLog(db_path=tmp_path / "audit.db")
    log.log_event(SAMPLE_EVENT)
    rows_before = log.read_all()
    log.log_event(dict(SAMPLE_EVENT, decision="ALLOW"))
    rows_after = log.read_all()
    assert len(rows_after) == len(rows_before) + 1  # append, not replace
    assert rows_after[0]["decision"] == "BLOCK"
    log.close()
