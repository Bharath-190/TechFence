"""Phase B integration tests (kit Prompt B-T).

Do NOT change source files from here; a failure means a real bug to report.
- Every file under data/ classifies to a superset of its registry labels
  (with the public-only false-positive check).
- Scenario C chain: employee_salary -> salary_summary -> external_api
  inherits EMPLOYEE_DATA/FINANCIAL; path_to equals the hardcoded chain.
- Persistence round-trip through a NEW sqlite connection.
"""

import sqlite3

import pytest

from taskfence import registry
from taskfence.audit import AuditLog
from taskfence.classifier import classify
from taskfence.lineage import LineageTracker

# Asset ids whose scanned content may only carry PUBLIC beyond the registry
# base labels (false-positive control, kit Prompt B-T).
PUBLIC_ONLY_ASSETS = {"public_company_info", "public_press_release"}


def test_every_data_file_classifies_to_superset_of_base_labels():
    for asset_id in registry.asset_ids():
        text = registry.read_asset(asset_id)
        labels = classify(asset_id, text)
        assert registry.base_labels(asset_id) <= labels, \
            (asset_id, labels)


@pytest.mark.parametrize("asset_id", sorted(PUBLIC_ONLY_ASSETS))
def test_public_files_classify_public_only(asset_id):
    labels = classify(asset_id, registry.read_asset(asset_id))
    assert labels == {"PUBLIC"}, (asset_id, labels)


def test_scenario_c_chain_labels_and_path(tmp_path):
    tracker = LineageTracker(db_path=tmp_path / "lin.db",
                             mode="conservative")
    task_id = "task-c"
    tracker.register_asset(task_id, "employee_salary")
    tracker.derive(task_id, ["employee_salary"], "average", "salary_summary")
    tracker.record_sink(task_id, "salary_summary", "external_api")

    labels = tracker.effective_labels(task_id, "external_api")
    assert {"EMPLOYEE_DATA", "FINANCIAL"} <= labels

    path = " -> ".join(tracker.path_to(task_id, "external_api"))
    assert path == "employee_salary -> salary_summary -> external_api"
    tracker.close()


def test_lineage_persists_to_new_connection(tmp_path):
    db = tmp_path / "lin.db"
    tracker = LineageTracker(db_path=db, mode="declared")
    tracker.register_asset("tX", "customer_db")
    tracker.derive("tX", ["customer_db"], "summarize", "customer_summary")
    tracker.close()

    conn = sqlite3.connect(db)  # a NEW, plain connection
    nodes = dict(conn.execute(
        "SELECT node, labels FROM lineage_nodes WHERE task_id = 'tX'"))
    edges = list(conn.execute(
        "SELECT src, dst, transformation FROM lineage_edges"
        " WHERE task_id = 'tX'"))
    conn.close()
    assert set(nodes) == {"customer_db", "customer_summary"}
    assert ("customer_db", "customer_summary", "summarize") in edges


def test_audit_and_lineage_coexist_in_one_db(tmp_path):
    db = tmp_path / "both.db"
    tracker = LineageTracker(db_path=db, mode="declared")
    log = AuditLog(db_path=db)
    tracker.register_asset("t1", "sales_report_q3")
    log.log_event({
        "task_id": "t1", "tool": "send_slack", "action": "send_message",
        "destination": "sales_slack", "decision": "ALLOW", "sources": [],
        "labels": [], "reasons": []})
    assert log.count() == 1
    tracker.close()
    log.close()
