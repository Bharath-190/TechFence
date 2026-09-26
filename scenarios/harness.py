"""Scenario harness (kit Prompts G1-G3).

Runs ScriptedAgent and direct calls through the REAL gateway app (same
policy engine, lineage, sinks), in-process via an ASGI adapter, with fully
isolated state per scenario: fresh SQLite DBs, fresh outbox, fresh in-memory
task maps. Attributes the gateway monkey-patches are saved and restored, so
scenarios never pollute each other or the test-suite globals.
"""

import copy
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.client import GatewayClient
from taskfence.gateway import app

ADMIN_TOKEN = "devtoken"
TASK = "Summarize Q3 sales and post it to #sales."


@dataclass
class ScenarioResult:
    name: str
    kind: str  # "unauthorized" | "legitimate"
    expectation: str
    passed: bool
    decisions: list[str] = field(default_factory=list)
    allowed: bool = False  # legitimate flow actually executed
    details: list[str] = field(default_factory=list)

    def as_row(self) -> tuple[str, str, str, str, str]:
        observed = ", ".join(self.decisions) or "-"
        verdict = "PASS" if self.passed else "FAIL"
        return (self.name, self.kind, self.expectation, observed, verdict)


class _ASGIAdapter:
    """httpx.Client-shaped wrapper delegating to the TestClient."""

    def __init__(self, test_client: TestClient):
        self._tc = test_client

    def post(self, url, json=None, **kwargs):
        return self._tc.post(url, json=json, **kwargs)

    def get(self, url, **kwargs):
        return self._tc.get(url, **kwargs)

    def put(self, url, **kwargs):
        return self._tc.put(url, **kwargs)

    def patch(self, url, **kwargs):
        return self._tc.patch(url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Harness:
    """Context manager: isolated gateway state for one scenario run."""

    def __init__(self, db_dir: Path | None = None):
        # A fresh temp dir per run: SQLite files must never accumulate
        # across scenarios, or audit counts would bleed between them.
        self.db_dir = (Path(db_dir) if db_dir
                       else Path(tempfile.mkdtemp(prefix="tf_scenario_")))
        self._owns_dir = db_dir is None
        self._saved: dict = {}

    def __enter__(self) -> "Harness":
        import taskfence.gateway as gw
        import taskfence.client as client_mod

        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._saved = {
            "gw": {name: getattr(gw, name) for name in
                   ("TRACKER", "AUDIT", "APPROVALS", "STATE", "CONTRACTS",
                    "TASK_TEXT", "LAST_DECISION")},
            "registry_outbox": registry.OUTBOX_DIR,
            "httpx_client": client_mod.httpx.Client,
            "env_token": os.environ.get("TASKFENCE_ADMIN_TOKEN"),
        }
        from taskfence.approvals import ApprovalStore
        from taskfence.audit import AuditLog, TaskStateStore
        from taskfence.lineage import LineageTracker

        gw.TRACKER = LineageTracker(db_path=self.db_dir / "lin.db")
        gw.AUDIT = AuditLog(self.db_dir / "audit.db")
        gw.APPROVALS = ApprovalStore(self.db_dir / "appr.db")
        gw.STATE = TaskStateStore(self.db_dir / "state.db")
        gw.CONTRACTS, gw.TASK_TEXT, gw.LAST_DECISION = {}, {}, {}
        registry.OUTBOX_DIR = self.db_dir / "outbox"
        registry.reset_outbox()
        os.environ["TASKFENCE_ADMIN_TOKEN"] = ADMIN_TOKEN

        test_client = TestClient(app)

        def _client_factory(timeout=None, **kwargs):
            return _ASGIAdapter(test_client)

        client_mod.httpx.Client = _client_factory
        self.test_client = test_client
        self.client = GatewayClient()
        self.client.test_client = test_client
        return self

    def __exit__(self, *exc) -> None:
        import taskfence.gateway as gw
        import taskfence.client as client_mod

        # Outbox hygiene: reset the ISOLATED outbox while OUTBOX_DIR still
        # points at the temp directory, so teardown can never truncate the
        # tracked repository sinks (tests/test_outbox_isolation.py pins
        # this). The old code reset AFTER restoring the repo path — which
        # emptied outbox/*.jsonl on every pytest run.
        registry.reset_outbox()

        if self._owns_dir:
            shutil.rmtree(self.db_dir, ignore_errors=True)

        saved = self._saved
        for name, value in saved["gw"].items():
            setattr(gw, name, value)
        registry.OUTBOX_DIR = saved["registry_outbox"]
        client_mod.httpx.Client = saved["httpx_client"]
        if saved["env_token"] is None:
            os.environ.pop("TASKFENCE_ADMIN_TOKEN", None)
        else:
            os.environ["TASKFENCE_ADMIN_TOKEN"] = saved["env_token"]

    # -- convenience assertions helpers -----------------------------------
    def sink_count(self, sink: str) -> int:
        return len(registry.sink_lines(sink))

    def audit_events(self) -> list[dict]:
        return self.test_client.get("/audit").json()["events"]


def result_dict(results: list[ScenarioResult]) -> list[dict]:
    return [copy.deepcopy(r.__dict__) | {"decisions": list(r.decisions)}
            for r in results]


def dump_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
