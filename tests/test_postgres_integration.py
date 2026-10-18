"""PostgreSQL integration tier (Phase 2; AD-19 tiers, §4.1–§4.4).

Every test in this module requires a real PostgreSQL server selected by
``TASKFENCE_TEST_DATABASE_URL`` and is deselected by default through the
``postgres_integration`` marker. A missing or unreachable configured test
database is a **failure**, never a skip and never a silent pass: run
``python scripts/postgres_preflight.py`` before pytest.

This tier is the only place where the approval-claim, taint-union,
constraint and transaction-boundary guarantees are accepted (§4.4): SQLite
serializes writes and its rowcount behaviour under contention is not
evidence of PostgreSQL row-level semantics.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from tests.child_helpers import (approve_task, postgres_env, resolve_concurrently,
                                 run_child, sink_lines, spawn_children, collect)

pytestmark = pytest.mark.postgres_integration

TASK_TEXT = "Summarize Q3 sales and post it to #sales."


@pytest.fixture(scope="module")
def pg_url():
    url = os.environ.get("TASKFENCE_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.fail("TASKFENCE_TEST_DATABASE_URL is not set — the "
                    "PostgreSQL tier cannot run. This is a preflight "
                    "failure, not a skip.")
    if not url.startswith(("postgresql://", "postgres://")):
        pytest.fail("TASKFENCE_TEST_DATABASE_URL must be a PostgreSQL URL, "
                    f"got {url.split(':', 1)[0]!r}")
    production = os.environ.get("TASKFENCE_DATABASE_URL", "").strip()
    if production and production == url:
        pytest.fail("TASKFENCE_TEST_DATABASE_URL must not be the "
                    "production URL")
    return url


@pytest.fixture()
def pg_persistence(pg_url):
    from taskfence.config import Config
    from taskfence.persistence import Persistence
    return Persistence(Config.from_env({"TASKFENCE_DATABASE_URL": pg_url}))


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _contract(contract_id: str, allowed_data: list[str] | None = None):
    from taskfence.models import TaskContract
    return TaskContract(
        contract_id=contract_id, purpose="sales_reporting",
        allowed_data=allowed_data or ["sales_reports"],
        allowed_destinations=["sales_slack"],
        allowed_actions=["read"], external_transfer=False,
        created_at="2026-10-17T00:00:00+00:00")


# --- 4.1 preflight and migration -------------------------------------------

def test_preflight_reaches_the_configured_database(pg_persistence):
    assert pg_persistence.config.use_postgres is True
    assert pg_persistence.db.backend == "postgres"
    pg_persistence.preflight()
    row = pg_persistence.db.query_one("SELECT current_database() AS name")
    assert row["name"]


def test_migration_is_idempotent_and_preserves_existing_rows(pg_url):
    from taskfence.config import Config
    from taskfence.persistence import Persistence
    config = Config.from_env({"TASKFENCE_DATABASE_URL": pg_url})
    store = Persistence(config)
    task_id = _uid("task")
    contract = _contract("c-migrate")
    store.tasks.create(task_id, "migrate me", contract)
    event_id = store.audit.log_event({
        "task_id": task_id, "task_text": "migrate me", "tool": "read_file",
        "action": "read", "sources": ["sales_report_q3"],
        "labels": ["CONFIDENTIAL"], "transformation": "", "destination": "",
        "decision": "ALLOW", "reasons": ["within scope"],
        "lineage_path": ["sales_report_q3"]})
    approval_id = store.approvals.create(
        task_id, "send_slack", {"request": "snapshot"},
        {"channel": "#sales"}, contract, ["customer_db"], ["customer_db"],
        ["soft rule"])
    store.lineage.register_asset(task_id, "sales_report_q3")

    # A second migration run (fresh process-equivalent store) is a no-op.
    again = Persistence(config)
    assert again.tasks.task_text(task_id) == "migrate me"
    assert again.audit.count(task_id) == 1
    assert again.approvals.get(approval_id)["status"] == "pending"
    assert again.lineage.session_asset_ids(task_id) == {"sales_report_q3"}
    assert event_id > 0
    versions = again.db.query(
        "SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == [1]

    # AD-03 / G-23: the reserved column set exists on the authoritative
    # backend and every reserved value is still NULL (no phase writes yet).
    from taskfence.persistence import _RESERVED_COLUMNS
    for table, column, _ in _RESERVED_COLUMNS:
        present = again.db.query(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s", (table, column))
        assert present, (table, column)
    reserved_values = again.db.query(
        "SELECT tenant_id, subject, trace_id, idempotency_key"
        " FROM audit_events WHERE task_id = %s", (task_id,))
    assert reserved_values == [{
        "tenant_id": None, "subject": None, "trace_id": None,
        "idempotency_key": None}]


def test_duplicate_keys_are_rejected_but_reads_are_idempotent(pg_persistence):
    task_id = _uid("task")
    contract = _contract("c-dup")
    pg_persistence.tasks.create(task_id, "dup", contract)
    with pytest.raises(Exception):
        pg_persistence.tasks.create(task_id, "dup again", contract)

    # Same contract version twice: the primary key rejects it.
    with pytest.raises(Exception):
        pg_persistence.tasks.set_contract(task_id, contract)

    # Same decision id twice: rejected.
    decision_id = pg_persistence.tasks.append_decision(
        task_id, contract.version, "read_file", "read", "", "ALLOW",
        ["within scope"], "fp-1")
    with pytest.raises(Exception):
        pg_persistence.db.execute(
            "INSERT INTO task_decisions (decision_id, task_id,"
            " contract_version, tool, action, destination, outcome,"
            " reasons_json, request_fingerprint, created_at, seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_decisions))",
            (decision_id, task_id, contract.version, "read_file", "read",
             "", "ALLOW", "[]", "fp-dup", "2026-10-17T00:00:00+00:00"))

    # Same (task_id, asset_id) taint registration: idempotent, no lost row.
    pg_persistence.lineage.register_asset(task_id, "customer_db")
    pg_persistence.lineage.register_asset(task_id, "customer_db")
    rows = pg_persistence.db.query(
        "SELECT asset_id FROM task_reads WHERE task_id = ?", (task_id,))
    assert [row["asset_id"] for row in rows] == ["customer_db"]


def test_contract_upgrade_is_atomic_or_leaves_no_partial_state(pg_persistence):
    task_id = _uid("task")
    first = _contract("c-atomic")
    pg_persistence.tasks.create(task_id, "atomic", first)

    second = _contract("c-atomic-x", ["sales_reports", "customer_db"])
    pg_persistence.tasks.set_contract(task_id, second)
    assert pg_persistence.tasks.current_version(task_id) == second.version

    # A failing upgrade (duplicate version) must not advance the pointer.
    with pytest.raises(Exception):
        pg_persistence.tasks.set_contract(task_id, second)
    assert pg_persistence.tasks.current_version(task_id) == second.version
    versions = pg_persistence.db.query(
        "SELECT contract_version FROM task_contracts WHERE task_id = ?",
        (task_id,))
    assert len(versions) == 2  # no partial contract row


# --- §4.4 concurrency evidence (PostgreSQL only) ---------------------------

def test_concurrent_approval_resolution_has_exactly_one_winner(tmp_path,
                                                              pg_url):
    """N=8 independent interpreters resolve the same approval at once:
    one 200, seven 409, exactly one outbox line."""
    env = postgres_env(pg_url, tmp_path)
    task_id, approval_id, _ = approve_task(env)

    results, lines = resolve_concurrently(env, approval_id, attempts=8)

    successes = [r for r in results if r["http"] == 200]
    conflicts = [r for r in results if r["http"] == 409]
    assert len(successes) == 1, results
    assert successes[0]["status"] == "allowed_once"
    assert len(conflicts) == 7, results
    assert all("already resolved" in str(c["detail"]) for c in conflicts)
    assert len(lines) == 1, lines
    assert lines[0]["task_id"] == task_id

    # The consumed approval stays consumed in a later process.
    later = run_child(env, "resolve", approval_id, "allow_once")
    assert later["http"] == 409


def test_concurrent_taint_registration_preserves_the_union(tmp_path, pg_url):
    """Concurrent `task_reads` registration never loses an update."""
    env = postgres_env(pg_url, tmp_path)
    task_id = run_child(env, "create_task", TASK_TEXT)["task_id"]
    assets = ["sales_report_q3", "sales_report_q2", "internal_strategy",
              "meeting_notes", "public_press_release", "deploy_notes"]
    envs = [dict(env) for _ in assets]
    args_list = [("tool_call", task_id, "read_file", json.dumps(
        {"asset_id": asset})) for asset in assets]
    results = collect(spawn_children(envs, args_list))
    assert all(r["outcome"] == "ALLOW" for r in results), results

    reads = run_child(env, "session_reads", task_id)["reads"]
    assert set(assets) <= set(reads)


def test_cross_process_contract_taint_and_negative_control(tmp_path, pg_url):
    env = postgres_env(pg_url, tmp_path)

    created = run_child(env, "create_task", TASK_TEXT)
    assert created["http"] == 200, created
    task_id = created["task_id"]

    allowed = run_child(env, "tool_call", task_id, "read_file",
                        json.dumps({"asset_id": "sales_report_q3"}))
    assert allowed["outcome"] == "ALLOW"

    blocked = run_child(env, "tool_call", task_id, "post_external",
                        json.dumps({
                            "url": "https://collect.example.invalid/ingest",
                            "payload": "customer data",
                            "source_assets": ["customer_db"],
                            "transformation": "export"}))
    assert blocked["http"] == 200, blocked
    assert blocked["outcome"] == "BLOCK"

    missing = run_child(env, "tool_call", "task-000000000000", "read_file",
                        json.dumps({"asset_id": "sales_report_q3"}))
    assert missing["http"] == 404  # negative control: database, not memory

    tainted_read = run_child(env, "tool_call", task_id, "query_customer_db",
                             json.dumps({}))
    assert tainted_read["outcome"] == "ALLOW"
    assert tainted_read["tainted"] is True

    send = run_child(env, "tool_call", task_id, "send_slack",
                     json.dumps({"channel": "#sales", "text": "Q3 summary",
                                 "source_assets": ["sales_report_q3"],
                                 "transformation": "summary"}))
    assert send["outcome"] in ("APPROVE", "BLOCK"), send  # never less strict

    history = run_child(env, "decisions", task_id)["decisions"]
    assert [row["tool"] for row in history] == [
        "read_file", "post_external", "query_customer_db", "send_slack"]
    assert [row["outcome"] for row in history] == [
        "ALLOW", "BLOCK", "ALLOW", history[-1]["outcome"]]


def test_decisions_are_append_only_on_postgres(pg_persistence):
    task_id = _uid("task")
    contract = _contract("c-append")
    pg_persistence.tasks.create(task_id, "append", contract)

    snapshots = []
    for index in range(8):
        pg_persistence.tasks.append_decision(
            task_id, contract.version, f"tool-{index}", "read", "", "ALLOW",
            [f"reason {index}"], f"fp-{index}")
        snapshots.append(pg_persistence.tasks.decisions(task_id))

    history = pg_persistence.tasks.decisions(task_id)
    assert len(history) == 8
    assert [row["tool"] for row in history] == [f"tool-{i}" for i in range(8)]
    # Every earlier row is unchanged, field-for-field:
    for index, earlier in enumerate(snapshots):
        assert history[:index + 1] == earlier
    assert pg_persistence.tasks.latest_decision(task_id)[
        "decision_id"] == history[-1]["decision_id"]


# --- G-16: one configured backend -------------------------------------------

def test_postgres_configuration_never_falls_back_to_sqlite(tmp_path, monkeypatch,
                                                          pg_url):
    """A configured PostgreSQL backend is authoritative: no SQLite file is
    created or read, even when a default-valued TASKFENCE_DB coexists."""
    monkeypatch.chdir(tmp_path)
    from taskfence.config import Config, PROJECT_ROOT
    from taskfence.persistence import Persistence
    config = Config.from_env({
        "TASKFENCE_DATABASE_URL": pg_url,
        "TASKFENCE_DB": "taskfence.sqlite3",
    })
    assert config.use_postgres is True
    assert config.sqlite_path == PROJECT_ROOT / "taskfence.sqlite3"

    persistence = Persistence(config)
    persistence.preflight()
    assert persistence.db.backend == "postgres"
    # Nothing SQLite-shaped is touched in the new working directory, and the
    # anchored default path is never opened by a security path.
    assert list(tmp_path.iterdir()) == []
    assert not Path(tmp_path / "taskfence.sqlite3").exists()
