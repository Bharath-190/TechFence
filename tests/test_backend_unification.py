"""One configured backend for gateway and dashboard (Phase 2; G-16, §3.4).

The gateway, the dashboard, the scenario harness and the tests build their
stores from a single `Persistence` factory created from one `Config`
snapshot, and a relative SQLite path is anchored once so the working
directory cannot change which file is used.
"""

import importlib
from pathlib import Path

import pytest

from taskfence import registry
from taskfence.config import Config, PROJECT_ROOT

TASKFENCE_SOURCES = sorted(Path("taskfence").glob("*.py"))


def test_gateway_and_dashboard_resolve_the_same_backend(tmp_path, monkeypatch):
    shared = tmp_path / "shared.sqlite3"
    monkeypatch.setenv("TASKFENCE_DB", str(shared))
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")

    from taskfence.config import load_config
    from taskfence.persistence import Persistence
    gateway_side = Persistence(load_config())

    # The dashboard is a separate process, started somewhere else.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    import dashboard.app as dash
    importlib.reload(dash)

    dashboard_side = dash._backend()
    assert gateway_side.db.backend == dashboard_side.db.backend
    assert gateway_side.db.sqlite_path == dashboard_side.db.sqlite_path
    assert dashboard_side.db.sqlite_path == shared


def test_relative_paths_are_anchored_not_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from taskfence.config import load_config
    assert load_config().sqlite_path == PROJECT_ROOT / "taskfence.sqlite3"
    monkeypatch.chdir(PROJECT_ROOT)
    assert load_config().sqlite_path == PROJECT_ROOT / "taskfence.sqlite3"


def test_no_security_module_opens_its_own_database():
    offenders = []
    for path in TASKFENCE_SOURCES:
        if path.name == "persistence.py":
            continue  # the single persistence module
        text = path.read_text(encoding="utf-8")
        if "sqlite3.connect(" in text:
            offenders.append((str(path), "sqlite3.connect"))
        if 'Path("taskfence.sqlite3")' in text:
            offenders.append((str(path), "hardcoded SQLite path"))
        if "DEFAULT_DB_PATH" in text:
            offenders.append((str(path), "DEFAULT_DB_PATH"))
    assert offenders == []


def test_dashboard_has_no_own_sqlite_connection():
    source = Path("dashboard/app.py").read_text(encoding="utf-8")
    assert "sqlite3.connect(" not in source
    assert "_ConnectionAdapter(_backend().db)" in source


@pytest.mark.parametrize("module_name", [
    "taskfence/audit.py", "taskfence/approvals.py", "taskfence/lineage.py",
])
def test_stores_take_the_shared_database(module_name):
    """Store constructors accept the shared Database (a bare path is only a
    legacy test convenience that resolves through the same factory)."""
    text = Path(module_name).read_text(encoding="utf-8")
    assert "coerce_database(" in text
    assert "sqlite3.connect(" not in text


def test_reserved_columns_exist_and_migrations_are_forward_only(tmp_path):
    """AD-03 / G-23: the full reserved column set is designed up front and
    added forward-only; a legacy database gains the columns without losing
    rows."""
    import sqlite3

    from taskfence.config import Config
    from taskfence.persistence import _RESERVED_COLUMNS, Persistence

    # A legacy database: the Phase 2 tables, without the reserved columns.
    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_text TEXT,"
        " created_at TEXT, current_contract_version TEXT);"
        "CREATE TABLE task_contracts (task_id TEXT, contract_version TEXT,"
        " contract_json TEXT, created_at TEXT,"
        " PRIMARY KEY (task_id, contract_version));"
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT, task_id TEXT, task_text TEXT, contract_id TEXT,"
        " contract_version TEXT, tool TEXT, action TEXT, sources TEXT,"
        " labels TEXT, transformation TEXT, destination TEXT,"
        " decision TEXT, reasons TEXT, lineage_path TEXT);"
        "CREATE TABLE approvals (approval_id TEXT PRIMARY KEY,"
        " task_id TEXT, tool TEXT, args TEXT, reasons TEXT, status TEXT,"
        " created_at TEXT, resolved_at TEXT);")
    conn.execute(
        "INSERT INTO audit_events (timestamp, task_id, task_text,"
        " contract_id, contract_version, tool, action, sources, labels,"
        " transformation, destination, decision, reasons, lineage_path)"
        " VALUES ('x', 't1', 'old', 'c', 'v', 'read_file', 'read', 's', 'l',"
        " 'none', '', 'ALLOW', 'r', 's')")
    conn.commit()
    conn.close()

    store = Persistence(Config.from_env({"TASKFENCE_DB": str(legacy)}))
    store.preflight()
    assert store.audit.count() == 1  # no data loss
    columns = {name for table, name, _ in _RESERVED_COLUMNS
               if table == "audit_events"}
    present = {row["name"] for row in store.db.query(
        "SELECT name FROM pragma_table_info('audit_events')")}
    assert columns <= present

    # Rerunning the migration is a no-op.
    Persistence(Config.from_env({"TASKFENCE_DB": str(legacy)})).preflight()
    assert store.audit.count() == 1
