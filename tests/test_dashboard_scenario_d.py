"""Dashboard Scenario D approval walkthrough tests (kit I4-T).

Drives the REAL Streamlit app (AppTest) against a REAL in-process gateway
(uvicorn Server, ephemeral port) with every store bound to a temporary DB
the dashboard also reads — no repo pollution. Asserts the full I4 loop:

1. clicking "Run Scenario D" issues the real scope-expansion request and
   renders the APPROVE decision + approval_id;
2. "Open approval preview" pulls the admin-gated approval-detail endpoint
   and renders the exact reviewed tool, tool arguments, source/data,
   destination, and contract id/version — straight from the response;
3. clicking "Allow Once" resolves via the existing endpoint, the dashboard
   shows the resulting ALLOW, and the seeded audit table gains the
   approved-execution row (approval_resolve / ALLOW) — the gateway's own
   live /audit endpoint reports the same events.

The dashboard owns no approval/security logic; this test fails if the
walkthrough stops calling the real gateway endpoints.
"""

import json
import re
import socket
import threading
from pathlib import Path

import httpx
import pytest

TASK = "Summarize Q3 sales and post it to #sales."
SCENARIO_D_ARGS = {"channel": "#sales", "text": "regional conversion: 42%",
                   "source_assets": ["customer_db"]}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def real_gateway(tmp_path, monkeypatch):
    """The real FastAPI app served by real uvicorn in-process, with every
    gateway store bound to tmp_path/taskfence.sqlite3 (the same DB the
    dashboard reads) and the outbox isolated to tmp_path. Yields base URL."""
    import uvicorn

    db = str(tmp_path / "taskfence.sqlite3")
    monkeypatch.setenv("TASKFENCE_DB", db)
    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    monkeypatch.setenv("TASKFENCE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("taskfence.contract._ollama_chat",
                        lambda task_text: None)
    from taskfence import registry
    monkeypatch.setattr(registry, "OUTBOX_DIR", tmp_path / "outbox")

    import taskfence.approvals as approvals_mod
    import taskfence.audit as audit_mod
    import taskfence.gateway as gw
    import taskfence.lineage as lineage_mod

    audit = audit_mod.AuditLog(db)
    state = audit_mod.TaskStateStore(db)
    approvals = approvals_mod.ApprovalStore(db)
    tracker = lineage_mod.LineageTracker(db_path=db, mode="conservative")
    monkeypatch.setattr(gw, "AUDIT", audit)
    monkeypatch.setattr(gw, "STATE", state)
    monkeypatch.setattr(gw, "APPROVALS", approvals)
    monkeypatch.setattr(gw, "TRACKER", tracker)

    config = uvicorn.Config(gw.app, host="127.0.0.1", port=0,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    base = (f"http://127.0.0.1:"
            f"{server.servers[0].sockets[0].getsockname()[1]}")
    # The dashboard script reads TASKFENCE_URL at each AppTest run, so it
    # must point at the REAL gateway from here on:
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


def _click(at, label, key=None):
    """Click a button by label; when duplicates exist (the pending-approvals
    cards also render Allow Once), prefer the requested streamlit key, else
    the first match (the walkthrough panel renders before the fragment)."""
    targets = [b for b in at.button if b.label == label]
    if key is not None and len(targets) > 1:
        keyed = [b for b in targets if getattr(b, "key", None) == key]
        targets = keyed or targets
    assert targets, f"button {label!r} not found"
    targets[0].click().run()
    assert not at.exception, at.exception


def test_scenario_d_walkthrough_allow_once(real_gateway):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path("dashboard/app.py").resolve()),
                           default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert "After resolution" not in " ".join(
        m.value or "" for m in at.markdown)  # nothing resolved yet

    # 1) Run Scenario D -> real APPROVE decision + approval_id rendered.
    _click(at, "Run Scenario D — approval walkthrough")
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "APPROVE" in markdown_text, markdown_text
    match = re.search(r"Approval ID: `([^`]+)`", markdown_text)
    assert match, markdown_text
    approval_id = match.group(1)
    assert approval_id
    task_match = re.search(r"Task: `([^`]+)`", markdown_text)
    assert task_match, markdown_text
    task_id = task_match.group(1)

    # 2) Open approval preview -> exact reviewed request from the endpoint.
    _click(at, "Open approval preview")
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    codes = " ".join(c.value or "" for c in at.code)
    assert "send_slack" in markdown_text            # exact reviewed tool
    assert "send_message" in markdown_text          # action
    assert "customer_db" in markdown_text           # declared source/data
    assert "sales_slack" in markdown_text           # destination
    # Reviewed contract id (c- + 12-hex hash suffix) and full version hash:
    assert re.search(r"Contract: `c-[0-9a-f]{12}` @ `[0-9a-f]{64}`",
                     markdown_text), markdown_text
    # Exact tool arguments, verbatim JSON from the approval response:
    assert json.dumps(SCENARIO_D_ARGS, indent=2, sort_keys=True) in codes

    # 3) Allow Once -> the resulting ALLOW is shown and audited.
    _click(at, "Allow Once", key="d-allow-once")
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "ALLOW</span>**" in markdown_text, markdown_text
    assert "executed: **True**" in markdown_text, markdown_text
    captions = " ".join(c.value or "" for c in at.caption)
    assert "Approved-execution lineage: customer_db" in captions, captions

    # The seeded audit table (dashboard's own DB view) gained the
    # approved-execution rows; the approval was consumed.
    audit_text = at.dataframe[0].value.to_string()
    assert "approval_resolve" in audit_text
    assert "ALLOW" in audit_text
    assert "send_slack" in audit_text
    assert len(at.warning) == 0  # no pending approvals left

    # The gateway's own live audit stream agrees, per task (auditability
    # end-to-end: APPROVE decision + its ALLOW resolution are recorded).
    events = httpx.get(f"{real_gateway}/audit",
                       params={"task_id": task_id},
                       timeout=10).json()["events"]
    assert [e["decision"] for e in events] == ["APPROVE", "ALLOW"], events
    assert events[0]["action"] == "send_message"
    assert events[1]["action"] == "approval_resolve"
    # Only ALLOWed execution delivered — exactly one slack sink line:
    from taskfence import registry
    assert len(registry.sink_lines("slack_sales")) == 1
    assert not registry.sink_lines("external_api")
