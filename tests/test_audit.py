"""Audit tests (kit Prompt B4): append-only, inspectable public surface."""

import inspect

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


def test_log_event_method_cannot_rewrite_history(tmp_path):
    log = AuditLog(db_path=tmp_path / "audit.db")
    log.log_event(SAMPLE_EVENT)
    rows_before = log.read_all()
    log.log_event(dict(SAMPLE_EVENT, decision="ALLOW"))
    rows_after = log.read_all()
    assert len(rows_after) == len(rows_before) + 1  # append, not replace
    assert rows_after[0]["decision"] == "BLOCK"
    log.close()
