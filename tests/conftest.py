"""Shared fixtures for agent/gateway tests: in-process ASGI transport.

The GatewayClient's httpx.Client is replaced with an adapter that talks to
the FastAPI TestClient directly, so agents run end-to-end with no network
and no live server. data/ stays reachable because we do NOT chdir.

Phase 2 (AD-21/AD-25): every gateway store is bound to ONE temporary
database, the same configuration the real process uses.
"""

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.config import Config
from taskfence.gateway import app
from taskfence.persistence import Database


class _ASGIAdapter:
    """httpx.Client-shaped wrapper delegating to the TestClient."""

    def __init__(self, test_client: TestClient):
        self._tc = test_client

    def post(self, url: str, json: dict | None = None, **kwargs):
        return self._tc.post(url, json=json, **kwargs)

    def get(self, url: str, **kwargs):
        return self._tc.get(url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def gateway_db(tmp_path):
    """One configured SQLite backend for the whole gateway."""
    return Database(Config.from_env(
        {"TASKFENCE_DB": str(tmp_path / "taskfence.sqlite3")}))


@pytest.fixture()
def isolate_gateway(tmp_path, monkeypatch, gateway_db):
    """Bind gateway globals to the single test database.

    Every store shares `gateway_db`, so the gateway under test behaves like
    the configured single-backend process. `registry.OUTBOX_DIR` and the
    local model are isolated too.
    """
    import taskfence.gateway as gw
    from taskfence.approvals import ApprovalStore
    from taskfence.audit import AuditLog, TaskStateStore
    from taskfence.lineage import LineageTracker
    from taskfence.persistence import TaskStore

    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(gw, "TASKS", TaskStore(gateway_db))
    monkeypatch.setattr(gw, "TRACKER", LineageTracker(gateway_db))
    monkeypatch.setattr(gw, "AUDIT", AuditLog(gateway_db))
    monkeypatch.setattr(gw, "APPROVALS", ApprovalStore(gateway_db))
    monkeypatch.setattr(gw, "STATE", TaskStateStore(gateway_db))
    # F2: expansion liveness is per process; every test starts with none.
    monkeypatch.setattr(gw, "LIVE_TASKS", gw.LIVE_TASKS.__class__())
    # Deterministic contract building: no network call to Ollama.
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
    registry.reset_outbox()
    yield gateway_db
    registry.reset_outbox()


@pytest.fixture()
def agent_client(isolate_gateway, monkeypatch):
    registry.reset_outbox()
    test_client = TestClient(app)

    def _client_factory(timeout=None, **kwargs):
        return _ASGIAdapter(test_client)

    monkeypatch.setattr("taskfence.client.httpx.Client", _client_factory)
    from taskfence.client import GatewayClient
    gateway_client = GatewayClient()
    gateway_client.test_client = test_client  # raw-HTTP escape hatch for tests
    yield gateway_client
    registry.reset_outbox()
