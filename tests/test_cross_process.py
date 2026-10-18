"""Cross-process and restart tests (Phase 2; G-01, G-02; §3.1, §3.2, §4.3).

Every statement below is executed by a **fresh interpreter**
(tests/proc_child.py), so the only way a result can be observed is through
the configured database — never shared process memory. These SQLite runs are
parity evidence; the concurrency guarantee itself is accepted only from the
PostgreSQL tier (tests/test_postgres_integration.py, AD-19/AD-25).
"""

import json

from tests.child_helpers import run_child, sqlite_env

TASK_TEXT = "Summarize Q3 sales and post it to #sales."


def test_contract_created_by_one_interpreter_is_usable_by_another(
        tmp_path):
    env = sqlite_env(tmp_path)

    # Interpreter A creates the task and stops.
    created = run_child(env, "create_task", TASK_TEXT)
    assert created["http"] == 200, created
    task_id = created["task_id"]

    # Interpreter B (no shared memory) uses A's persisted contract:
    allowed = run_child(env, "tool_call", task_id, "read_file",
                        json.dumps({"asset_id": "sales_report_q3"}))
    assert allowed["http"] == 200, allowed
    assert allowed["outcome"] == "ALLOW"

    # A destination outside the contract is BLOCK — not a 404, not an error.
    blocked = run_child(env, "tool_call", task_id, "post_external",
                        json.dumps({
                            "url": "https://collect.example.invalid/ingest",
                            "payload": "customer data",
                            "source_assets": ["customer_db"],
                            "transformation": "export"}))
    assert blocked["http"] == 200, blocked
    assert blocked["outcome"] == "BLOCK"


def test_negative_control_unknown_task_is_404_in_a_fresh_interpreter(
        tmp_path):
    """A second interpreter that reads the database — not inherited state —
    must not see a task that was never created there."""
    env = sqlite_env(tmp_path)
    missing = run_child(env, "tool_call", "task-000000000000", "read_file",
                        json.dumps({"asset_id": "sales_report_q3"}))
    assert missing["http"] == 404
    assert missing["outcome"] is None


def test_taint_survives_restart_and_cross_process_execution(tmp_path):
    """Control flow is ALLOW; after one interpreter taints the session, a
    second interpreter's outbound decision is strictly more restrictive."""
    control_env = sqlite_env(tmp_path / "control")
    control_task = run_child(control_env, "create_task", TASK_TEXT)["task_id"]
    control_send = run_child(
        control_env, "tool_call", control_task, "send_slack",
        json.dumps({"channel": "#sales", "text": "Q3 summary",
                    "source_assets": ["sales_report_q3"],
                    "transformation": "summary"}))
    assert control_send["outcome"] == "ALLOW", control_send
    assert run_child(control_env, "session_reads", control_task)["reads"] == []

    env = sqlite_env(tmp_path / "tainted")
    task_id = run_child(env, "create_task", TASK_TEXT)["task_id"]

    # Interpreter A reads in scope, then out of scope: the second read is
    # allowed but taints the session.
    in_scope = run_child(env, "tool_call", task_id, "read_file",
                         json.dumps({"asset_id": "sales_report_q3"}))
    assert in_scope["outcome"] == "ALLOW"
    tainted_read = run_child(env, "tool_call", task_id, "query_customer_db",
                             json.dumps({}))
    assert tainted_read["outcome"] == "ALLOW"
    assert tainted_read["tainted"] is True

    # Interpreter B (a restart of the process, and a second process at once)
    # sees the persisted taint and its outbound decision escalates.
    send = run_child(env, "tool_call", task_id, "send_slack",
                     json.dumps({"channel": "#sales", "text": "Q3 summary",
                                 "source_assets": ["sales_report_q3"],
                                 "transformation": "summary"}))
    assert send["outcome"] in ("APPROVE", "BLOCK"), send
    assert send["outcome"] != "ALLOW"  # never less restrictive than control

    # The taint itself is durable and visible to another interpreter.
    reads = run_child(env, "session_reads", task_id)["reads"]
    assert "customer_db" in reads
    assert "sales_report_q3" in reads


def test_decision_history_order_is_stable_across_processes(tmp_path):
    env = sqlite_env(tmp_path)
    task_id = run_child(env, "create_task", TASK_TEXT)["task_id"]
    for tool, args in (
            ("read_file", {"asset_id": "sales_report_q3"}),
            ("query_customer_db", {}),
            ("send_slack", {"channel": "#sales", "text": "Q3 summary",
                            "source_assets": ["customer_db"]})):
        run_child(env, "tool_call", task_id, tool, json.dumps(args))

    history = run_child(env, "decisions", task_id)["decisions"]
    assert [row["tool"] for row in history] == [
        "read_file", "query_customer_db", "send_slack"]
    assert [row["outcome"] for row in history] == [
        "ALLOW", "ALLOW", "APPROVE"]
    # A fresh interpreter derives the same "latest" row:
    assert history[-1]["decision_id"] == run_child(
        env, "decisions", task_id)["decisions"][-1]["decision_id"]


def test_taint_survives_an_in_process_restart_of_the_gateway(tmp_path):
    """Read → rebuild every store from the same database (restart) →
    outbound is still tainted. No path may reduce the label set."""
    from taskfence.config import Config
    from taskfence.lineage import LineageTracker

    env = sqlite_env(tmp_path)
    task_id = run_child(env, "create_task", TASK_TEXT)["task_id"]
    config = Config.from_env({"TASKFENCE_DB": env["TASKFENCE_DB"]})

    before = LineageTracker(db=None, db_path=config.sqlite_path)
    before.register_asset(task_id, "customer_db")
    assert before.session_asset_ids(task_id) == {"customer_db"}
    labels_before = before.effective_labels(task_id, "outbound_payload")
    before.close()

    # A restart: new object, no shared memory, same database.
    after = LineageTracker(db=None, db_path=config.sqlite_path)
    assert after.session_asset_ids(task_id) == {"customer_db"}
    labels_after = after.effective_labels(task_id, "outbound_payload")
    after.close()
    assert labels_before <= labels_after  # never less restrictive
    assert {"CUSTOMER_DATA", "PII", "FINANCIAL"} <= labels_after
