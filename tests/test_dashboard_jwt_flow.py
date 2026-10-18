"""Phase 3 (G-04): the dashboard's REAL approval workflow against a
JWT-enabled gateway.

Drives the real Streamlit app (AppTest) against the real gateway served by
real uvicorn in-process, with JWT mode enabled and every store bound to a
temporary DB the dashboard also reads. Nothing about the gateway's
authentication is stubbed: the 401/403/200 outcomes asserted here are decided
by `taskfence.gateway._authenticate` itself.

Covered:
- Approve/Deny workflow succeeds with a valid JWT and a valid admin token;
- a missing reviewer token and an expired reviewer token are reported as 401
  and resolve NOTHING (no false success);
- a missing/invalid admin token is reported as 403 while the JWT verifies;
- neither the admin token nor the reviewer JWT ever reaches rendered content
  or the logs.

The token is supplied through TASKFENCE_REVIEWER_JWT, the manual flow that
this phase supports (there is no in-dashboard login yet).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import jwt as pyjwt
import pytest

from tests.jwt_keys import TEST_PRIVATE_KEY, TEST_PUBLIC_KEY

APP_PATH = Path("dashboard/app.py").resolve()
ADMIN = "s3cret-admin-token"
# A second, distinct admin token for the out-of-process gateway. The mismatch
# test needs the two processes to hold different values, which one shared
# environment cannot express.
GATEWAY_ADMIN = "gateway-expected-token"
STALE_ADMIN = "dashboard-stale-token"
ISSUER = "https://issuer.test/"
AUDIENCE = "taskfence-api"
SUBJECT = "auth0|dashboard-reviewer"
TASK_TEXT = "Summarize Q3 sales and post it to #sales."
OUTBOUND_ARGS = {"channel": "#sales", "text": "regional conversion summary",
                 "source_assets": ["customer_db"]}


def _token(exp_offset: int = 300) -> str:
    now = int(time.time())
    return pyjwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT,
                         "exp": now + exp_offset, "iat": now - 10,
                         "jti": "dashboard-flow-1"}, TEST_PRIVATE_KEY,
                        algorithm="RS256")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def jwt_gateway(tmp_path, monkeypatch):
    """Real uvicorn gateway in JWT mode + a dashboard pointed at it.

    Admin token, reviewer JWT and key material live in the environment of the
    two processes under test, never in rendered content.
    """
    import uvicorn

    db = str(tmp_path / "taskfence.sqlite3")
    env = {
        "TASKFENCE_DB": db,
        "TASKFENCE_ADMIN_TOKEN": ADMIN,
        "TASKFENCE_AUTH_MODE": "jwt",
        "TASKFENCE_JWT_PUBLIC_KEY": TEST_PUBLIC_KEY,
        "TASKFENCE_JWT_ALLOWED_ALGS": "RS256",
        "TASKFENCE_JWT_ISSUER": ISSUER,
        "TASKFENCE_JWT_AUDIENCE": AUDIENCE,
        "TASKFENCE_OLLAMA_URL": "http://127.0.0.1:1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TASKFENCE_REVIEWER_JWT", raising=False)
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
    from taskfence import registry
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")

    import taskfence.approvals as approvals_mod
    import taskfence.audit as audit_mod
    import taskfence.gateway as gw
    import taskfence.lineage as lineage_mod
    from taskfence.auth import JWTVerifier
    from taskfence.config import Config

    audit = audit_mod.AuditLog(db)
    state = audit_mod.TaskStateStore(db)
    approvals = approvals_mod.ApprovalStore(db)
    tracker = lineage_mod.LineageTracker(db_path=db, mode="conservative")
    monkeypatch.setattr(gw, "AUDIT", audit)
    monkeypatch.setattr(gw, "STATE", state)
    monkeypatch.setattr(gw, "APPROVALS", approvals)
    monkeypatch.setattr(gw, "TRACKER", tracker)
    # JWT mode for the gateway app: the endpoints read these module globals on
    # every request, so this is exactly the configured-at-startup state.
    config = Config.from_env(env)
    monkeypatch.setattr(gw, "CONFIG", config)
    monkeypatch.setattr(gw, "JWT", JWTVerifier.from_config(config))

    server = uvicorn.Server(uvicorn.Config(gw.app, host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    base = (f"http://127.0.0.1:"
            f"{server.servers[0].sockets[0].getsockname()[1]}")
    monkeypatch.setenv("TASKFENCE_URL", base)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        audit.close()
        state.close()
        approvals.close()
        tracker.close()


@pytest.fixture()
def stale_admin_gateway(tmp_path, monkeypatch):
    """JWT gateway in its OWN process holding an admin token the dashboard
    does not have.

    Both processes read `TASKFENCE_ADMIN_TOKEN` from their own environment,
    so this is the only way to produce a genuine (non-empty, wrong) admin
    token end to end. The gateway starts from a real environment with no
    monkeypatching, and its database is the same temp file the dashboard
    reads. The test only denies, so no tool executes and no sink is touched.
    """
    port = _free_port()
    db = tmp_path / "taskfence.sqlite3"
    gateway_env = {
        **os.environ,
        "TASKFENCE_DB": str(db),
        "TASKFENCE_ADMIN_TOKEN": GATEWAY_ADMIN,
        "TASKFENCE_AUTH_MODE": "jwt",
        "TASKFENCE_JWT_PUBLIC_KEY": TEST_PUBLIC_KEY,
        "TASKFENCE_JWT_ALLOWED_ALGS": "RS256",
        "TASKFENCE_JWT_ISSUER": ISSUER,
        "TASKFENCE_JWT_AUDIENCE": AUDIENCE,
        "TASKFENCE_OLLAMA_URL": "http://127.0.0.1:1",
    }
    log = (tmp_path / "gateway.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "taskfence.gateway:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        env=gateway_env, stdout=log, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent.parent))
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base, process, tmp_path / "gateway.log")
        # The dashboard process now holds a DIFFERENT admin token.
        monkeypatch.setenv("TASKFENCE_URL", base)
        monkeypatch.setenv("TASKFENCE_DB", str(db))
        monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", STALE_ADMIN)
        for key in ("TASKFENCE_AUTH_MODE", "TASKFENCE_JWT_PUBLIC_KEY",
                    "TASKFENCE_JWT_ALLOWED_ALGS", "TASKFENCE_JWT_ISSUER",
                    "TASKFENCE_JWT_AUDIENCE"):
            monkeypatch.setenv(key, gateway_env[key])
        monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", _token())
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


def _wait_until_ready(base: str, process, log_path: Path) -> None:
    """Wait for the real server; a process that dies is reported with its
    own log rather than a bare timeout."""
    import httpx

    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"gateway exited ({process.returncode}): "
                f"{log_path.read_text(errors='replace')[-2000:]}")
        try:
            if httpx.get(f"{base}/audit", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"gateway not ready in 30s: "
                         f"{log_path.read_text(errors='replace')[-2000:]}")


def _seed_pending_approval(base: str) -> tuple[str, str]:
    """Create a real pending approval through the gateway's own API."""
    import httpx

    with httpx.Client(timeout=30, base_url=base) as client:
        task_id = client.post(
            "/tasks", json={"task_text": TASK_TEXT}).json()["task_id"]
        body = client.post(
            f"/tasks/{task_id}/tool-call",
            json={"tool": "send_slack", "args": OUTBOUND_ARGS}).json()
    assert body["decision"]["outcome"] == "APPROVE", body
    return task_id, body["approval_id"]


def _run_dashboard():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def _click(at, label: str, key: str | None = None):
    targets = [b for b in at.button if b.label == label]
    if key is not None:
        keyed = [b for b in targets if getattr(b, "key", None) == key]
        targets = keyed or targets
    assert targets, f"button {label!r} not found"
    targets[0].click().run()
    assert not at.exception, at.exception
    return at


def _errors(at) -> list[str]:
    return [e.value or "" for e in at.error]


def _resolve_failures(at) -> list[str]:
    """Only the messages the resolve path itself emits (the SYSTEM STATUS
    warning also mentions the variable name, so it must not be counted)."""
    return [e for e in _errors(at) if e.startswith("Resolve failed")]


def _rendered_text(at) -> str:
    """Everything a browser would receive, across every element type."""
    parts = []
    for kind in ("markdown", "code", "text", "error", "warning", "info",
                 "caption", "success", "toast", "title", "header",
                 "subheader", "json"):
        for element in getattr(at, kind, None) or []:
            parts.append(str(getattr(element, "value", "")))
    return "\n".join(parts)


def _pending(base: str) -> list[dict]:
    import httpx

    with httpx.Client(timeout=10, base_url=base) as client:
        return client.get("/approvals").json()["pending"]


def _status(base: str, approval_id: str) -> str:
    if any(a["approval_id"] == approval_id for a in _pending(base)):
        return "pending"
    return "resolved"


# --------------------------------------------------------------------------
# Valid credentials: the workflow works end to end.
# --------------------------------------------------------------------------

def test_dashboard_deny_succeeds_with_valid_jwt(jwt_gateway, monkeypatch):
    """A valid JWT plus a valid admin token resolves through the real UI and
    is reported as done."""
    monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", _token())
    task_id, approval_id = _seed_pending_approval(jwt_gateway)

    at = _run_dashboard()
    _click(at, "Deny", key=approval_id + "d")

    assert _resolve_failures(at) == [], _errors(at)
    assert any("Approval deny" in (t.value or "") for t in at.toast)
    assert _status(jwt_gateway, approval_id) == "resolved"

    # The reviewer identity reached the gateway's audit trail; the trail stays
    # unreadable without credentials.
    import httpx
    with httpx.Client(timeout=10, base_url=jwt_gateway) as client:
        public = client.get("/audit", params={"task_id": task_id}).json()
        authenticated = client.get(
            "/audit", params={"task_id": task_id},
            headers={"X-Admin-Token": ADMIN,
                     "Authorization": f"Bearer {_token()}"}).json()
    assert "subject" not in json.dumps(public)
    assert SUBJECT in json.dumps(authenticated)


# --------------------------------------------------------------------------
# Reviewer token problems: reported clearly, and nothing is resolved.
# --------------------------------------------------------------------------

def test_missing_reviewer_jwt_is_reported_and_resolves_nothing(
        jwt_gateway, monkeypatch):
    monkeypatch.delenv("TASKFENCE_REVIEWER_JWT", raising=False)
    _, approval_id = _seed_pending_approval(jwt_gateway)

    at = _run_dashboard()
    _click(at, "Deny", key=approval_id + "d")

    failures = _resolve_failures(at)
    assert failures, _errors(at)
    assert "401" in failures[0] and "TASKFENCE_REVIEWER_JWT" in failures[0], \
        failures
    # No false success, and the approval is untouched.
    assert not any("done" in (t.value or "") for t in at.toast)
    assert _status(jwt_gateway, approval_id) == "pending"


def test_expired_reviewer_jwt_is_reported_and_resolves_nothing(
        jwt_gateway, monkeypatch):
    monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", _token(exp_offset=-60))
    _, approval_id = _seed_pending_approval(jwt_gateway)

    at = _run_dashboard()
    _click(at, "Deny", key=approval_id + "d")

    failures = _resolve_failures(at)
    assert failures and "401" in failures[0], _errors(at)
    assert not any("done" in (t.value or "") for t in at.toast)
    assert _status(jwt_gateway, approval_id) == "pending"


def test_missing_admin_token_is_reported_as_403(jwt_gateway, monkeypatch):
    """The JWT verifies but authorization fails: the dashboard must say 403
    and must not imply the approval was decided."""
    monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", _token())
    monkeypatch.delenv("TASKFENCE_ADMIN_TOKEN", raising=False)
    _, approval_id = _seed_pending_approval(jwt_gateway)

    at = _run_dashboard()
    _click(at, "Deny", key=approval_id + "d")

    failures = _resolve_failures(at)
    assert failures and "403" in failures[0], _errors(at)
    assert not any("done" in (t.value or "") for t in at.toast)
    assert _status(jwt_gateway, approval_id) == "pending"


def test_mismatched_admin_token_is_reported_as_403(stale_admin_gateway):
    """A non-empty but WRONG admin token (a second, separately configured
    gateway process) must produce 403, no success indication, and no change
    to the approval."""
    import httpx

    _, approval_id = _seed_pending_approval(stale_admin_gateway)

    at = _run_dashboard()
    _click(at, "Deny", key=approval_id + "d")

    failures = _resolve_failures(at)
    assert failures and "403" in failures[0], _errors(at)
    assert not any("done" in (t.value or "") for t in at.toast)
    assert _status(stale_admin_gateway, approval_id) == "pending"

    # Control: the same gateway accepts the reviewer JWT with the token it
    # actually expects, so the 403 above came from the mismatch and not from
    # an unavailable or broken endpoint.
    with httpx.Client(timeout=10, base_url=stale_admin_gateway) as client:
        accepted = client.post(
            f"/approvals/{approval_id}/resolve", json={"choice": "deny"},
            headers={"X-Admin-Token": GATEWAY_ADMIN,
                     "Authorization": f"Bearer {_token()}"})
    assert accepted.status_code == 200, accepted.text


# --------------------------------------------------------------------------
# Credentials stay server-side.
# --------------------------------------------------------------------------

def test_credentials_never_reach_rendered_content_or_logs(
        jwt_gateway, monkeypatch, caplog):
    import logging

    token = _token()
    monkeypatch.setenv("TASKFENCE_REVIEWER_JWT", token)
    _seed_pending_approval(jwt_gateway)

    caplog.set_level(logging.DEBUG)
    at = _run_dashboard()
    pending = _pending(jwt_gateway)
    assert pending, "expected a pending approval"
    _click(at, "Deny", key=pending[0]["approval_id"] + "d")

    rendered = _rendered_text(at) + caplog.text
    assert ADMIN not in rendered
    assert token not in rendered
    assert TEST_PRIVATE_KEY not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered
