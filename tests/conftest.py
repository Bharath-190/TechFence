"""Shared fixtures for agent/gateway tests: in-process ASGI transport.

The GatewayClient's httpx.Client is replaced with an adapter that talks to
the FastAPI TestClient directly, so agents run end-to-end with no network
and no live server. data/ stays reachable because we do NOT chdir.
"""

import pytest
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.gateway import app


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
def agent_client(tmp_path, monkeypatch):
    # Isolate mutable state WITHOUT chdir (data/ paths are CWD-relative).
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")
    import taskfence.gateway as gw
    from taskfence.audit import AuditLog
    from taskfence.lineage import LineageTracker
    monkeypatch.setattr(gw, "TRACKER",
                        LineageTracker(db_path=tmp_path / "lin.db"))
    monkeypatch.setattr(gw, "AUDIT", AuditLog(tmp_path / "audit.db"))
    from taskfence.approvals import ApprovalStore
    monkeypatch.setattr(gw, "APPROVALS", ApprovalStore(tmp_path / "appr.db"))
    # Deterministic contract building: no network call to Ollama.
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
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
