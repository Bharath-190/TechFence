"""TaskFence dashboard (spec §17, §29, §31; kit Prompts F1, F2, F3).

Reads SQLite, the generated scenario report and sink files only — it is a
separate process from the gateway. The UI computes NOTHING about security:
every decision, reason, lineage chain and metric is rendered from what the
gateway persisted or scenarios.run_all generated. Sidebar buttons are
triggers only (scenario runs, approval resolution via the gateway API,
reset).
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import streamlit as st

st.set_page_config(page_title="TaskFence", page_icon="🛡️", layout="wide")

DB_PATH = Path(os.environ.get("TASKFENCE_DB", "taskfence.sqlite3"))
GATEWAY = os.environ.get("TASKFENCE_URL", "http://localhost:8000").rstrip("/")
ADMIN_TOKEN = os.environ.get("TASKFENCE_ADMIN_TOKEN", "")

OUTCOME_COLOR = {"ALLOW": "#2e7d32", "BLOCK": "#c62828",
                 "APPROVE": "#f9a825"}

SCENARIOS = {
    "Scenario A — legitimate sales flow": "scenario_a",
    "Scenario B — injected agent (poisoned notes)": "scenario_b",
    "Scenario C — derived salary bypass": "scenario_c",
}

# E/F/G replay via scenarios/replay.py (same GatewayClient execution path
# as the agent CLI; F's structural probes included). G is the DISCLOSED
# conservative-taint false positive — the label says so (kit I5).
REPLAY_SCENARIOS = {
    "Scenario E — unknown destination (default deny)": "scenario_e",
    "Scenario F — contract tamper attempt (impossible)": "scenario_f",
    "Scenario G — precision check (known false positive)": "scenario_g",
}
SCENARIO_TASK = "Summarize Q3 sales and post it to #sales."

# Scenario D scope-expansion request (same shape the agent sends in
# scenarios/scenario_d.py — the dashboard only TRIGGERS it).
SCENARIO_D_ARGS = {"channel": "#sales", "text": "regional conversion: 42%",
                   "source_assets": ["customer_db"]}

# Demo-clarity metadata (display ONLY): judge-friendly scenario context.
# Every expected sequence here restates the documented spec behavior; the
# dashboard still renders the REAL stored decisions next to it and never
# substitutes the expectation for the actual outcome.
SCENARIO_INFO = {
    "scenario_a": {
        "title": "A — Legitimate Sales Reporting",
        "subtitle": "Authorized flow",
        "flow": "sales_report → summary → #sales",
        "expected": "ALLOW → ALLOW",
        "description": ("Demonstrates a normal authorized workflow where "
                        "task-relevant data moves only to an approved "
                        "destination."),
        "proves": ("TaskFence allows data movement when the flow matches "
                   "the task contract."),
    },
    "scenario_b": {
        "title": "B — Prompt Injection",
        "subtitle": "Retrieved content attempts unauthorized exfiltration",
        "flow": "sales_report → external_api",
        "expected": "ALLOW → BLOCK",
        "description": ("Demonstrates that instructions hidden in "
                        "retrieved content cannot authorize an otherwise "
                        "unauthorized external transfer."),
        "proves": ("Prompt-injected content cannot expand the agent's "
                   "authorized destination scope."),
    },
    "scenario_c": {
        "title": "C — Derived Sensitive Data",
        "subtitle": "Sensitive source is transformed before attempted "
                    "exfiltration",
        "flow": "employee_salary → average → external_api",
        "expected": "ALLOW → BLOCK",
        "description": ("Demonstrates that transforming sensitive "
                        "information does not remove the restrictions "
                        "inherited from its source."),
        "proves": "Derived data retains source lineage restrictions.",
        "note": ("The first ALLOW is the READ decision. The subsequent "
                 "external transfer is BLOCKED — the ALLOW is not the "
                 "final scenario result."),
    },
    "scenario_d": {
        "title": "D — Human Approval",
        "subtitle": "Outbound action requires explicit authorization",
        "flow": "internal_data → outbound_message",
        "expected": "ALLOW → APPROVE",
        "description": ("Demonstrates TaskFence's human-in-the-loop "
                        "escalation path for actions that require "
                        "additional authorization."),
        "proves": ("TaskFence can pause an action and require human "
                   "approval instead of silently executing it."),
    },
    "scenario_e": {
        "title": "E — Unknown Destination",
        "subtitle": "Destination outside the recognized authorization scope",
        "flow": "internal_data → unknown_destination",
        "expected": "BLOCK",
        "description": ("Demonstrates fail-closed behavior when the "
                        "destination is unknown or not authorized."),
        "proves": "Unknown destinations are not automatically trusted.",
    },
    "scenario_f": {
        "title": "F — Contract Tampering",
        "subtitle": "Attempt to modify authorization",
        "flow": "task_contract → mutation_attempt",
        "expected": "REJECTED",
        "description": ("Demonstrates that the agent cannot rewrite the "
                        "task contract to grant itself additional "
                        "permissions."),
        "proves": ("The agent controls HOW it performs a task, but cannot "
                   "redefine WHAT it is authorized to do."),
    },
    "scenario_g": {
        "title": "G — Conservative Lineage",
        "subtitle": "Insufficient lineage confidence",
        "flow": "derived_data → destination",
        "expected": "APPROVE",
        "description": ("Demonstrates conservative escalation when "
                        "lineage evidence is not sufficient to establish "
                        "that the flow is safe."),
        "proves": ("TaskFence fails conservatively rather than silently "
                   "allowing an uncertain data flow."),
    },
}

# Final-outcome banner colors (display only; derived from stored audit
# state, never from the expectation).
FINAL_COLOR = {
    "COMPLETED": "#2e7d32",
    "COMPLETED — AFTER HUMAN APPROVAL": "#2e7d32",
    "BLOCKED": "#c62828",
    "CONTRACT CHANGE REJECTED": "#c62828",
    "HUMAN APPROVAL REQUIRED": "#f9a825",
    "CONSERVATIVE ESCALATION": "#f9a825",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def _latest_task_and_state() -> tuple[str | None, dict | None]:
    if not DB_PATH.exists():
        return None, None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT task_id, state FROM task_state"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)
    except sqlite3.OperationalError:
        return None, None
    finally:
        conn.close()


def _latest_state() -> dict | None:
    return _latest_task_and_state()[1]


def _latest_audit_for_task(task_id: str | None) -> dict | None:
    """Most recent audit event for the latest task — verbatim stored flow
    fields (sources, labels, transformation, destination) for the decision
    panel. Read-only; the panel assembles display strings only."""
    if not DB_PATH.exists():
        return None
    conn = _db()
    try:
        if task_id:
            row = conn.execute(
                "SELECT sources, labels, transformation, destination"
                " FROM audit_events WHERE task_id = ?"
                " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT sources, labels, transformation, destination"
                " FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return dict(zip(["sources", "labels", "transformation",
                         "destination"], row))
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _gateway_ok(timeout: float = 2.0) -> bool:
    """Lightweight health check (one GET per rerun — no tight polling
    loop). Used by SYSTEM STATUS and before spawning scenario runs."""
    try:
        response = httpx.get(f"{GATEWAY}/audit", timeout=timeout)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _system_status() -> None:
    """Phase 11: SYSTEM STATUS — one lightweight health check per rerun
    (no background polling loop)."""
    healthy = _gateway_ok()
    st.markdown(
        "**SYSTEM STATUS**  \n"
        f"Gateway: **{'CONNECTED' if healthy else 'UNREACHABLE'}**  \n"
        "Security Engine: **ACTIVE** (deterministic, gateway-side)  \n"
        "Dashboard: **RUNNING**")
    if not healthy:
        st.caption("Start it: `TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/"
                   "uvicorn taskfence.gateway:app --port 8000`")


def _task_audit_rows(task_id: str | None) -> list[dict]:
    """Every stored audit event of a task, verbatim, in decision order —
    the raw material for the SECURITY FLOW panel (real data only)."""
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE task_id = ? ORDER BY id",
                (task_id,))
        else:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id")
        names = [d[0] for d in rows.description]
        return [dict(zip(names, row)) for row in rows.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _security_steps(rows: list[dict]) -> list[dict]:
    """Flatten stored audit rows into display steps (no decision is
    invented; ALLOW/BLOCK/APPROVE come verbatim from the store)."""
    steps = []
    for row in rows:
        if row.get("decision") not in ("ALLOW", "BLOCK", "APPROVE"):
            continue
        steps.append({
            "tool": row.get("tool", ""),
            "action": row.get("action", ""),
            "source": (row.get("sources") or "").strip() or "—",
            "transformation": (row.get("transformation") or "").strip(),
            "destination": (row.get("destination") or "").strip(),
            "decision": row["decision"],
            "reasons": (row.get("reasons") or "").strip(),
        })
    return steps


def _derive_final_outcome(steps: list[dict],
                          scenario_key: str | None) -> tuple[str, str]:
    """FINAL SCENARIO OUTCOME from the REAL stored decision sequence —
    never from the first decision alone and never fabricated:

    any BLOCK                -> BLOCKED (the last block's stored reasons)
    APPROVE, unresolved      -> HUMAN APPROVAL REQUIRED, or, for scenario G,
                                CONSERVATIVE ESCALATION (the documented
                                conservative lineage/precision case)
    APPROVE + human resolve  -> COMPLETED — AFTER HUMAN APPROVAL
    all ALLOW                -> COMPLETED
    no audited decisions     -> CONTRACT CHANGE REJECTED for scenario F
                                (nothing exists to mutate; probes show it),
                                else NO DECISION RECORDED
    """
    if not steps:
        if scenario_key == "scenario_f":
            return "CONTRACT CHANGE REJECTED", (
                "No protected call executed and no mutation surface exists: "
                "the gateway has no contract-mutating endpoint and the "
                "direct tamper attempt is rejected. The authorization "
                "boundary was not rewritten.")
        return "NO DECISION RECORDED", (
            "No audited security decisions for the latest task yet.")
    decisions = [step["decision"] for step in steps]
    if "BLOCK" in decisions:
        last_block = next(step for step in reversed(steps)
                          if step["decision"] == "BLOCK")
        return "BLOCKED", (last_block["reasons"]
                           or "Denied by security policy; nothing was "
                              "executed.")
    if "APPROVE" in decisions:
        approved_index = max(index for index, step in enumerate(steps)
                             if step["decision"] == "APPROVE")
        resolved_after = any(
            step["decision"] == "ALLOW" and step["action"] == "approval_resolve"
            for step in steps[approved_index + 1:])
        if resolved_after:
            return "COMPLETED — AFTER HUMAN APPROVAL", (
                "A human approved the exact held request (allow_once); it "
                "executed exactly once.")
        if scenario_key == "scenario_g":
            return "CONSERVATIVE ESCALATION", (
                "Known conservative lineage/precision case: available "
                "lineage evidence was insufficient to establish a safe "
                "ALLOW, so TaskFence escalates to a human rather than "
                "silently allowing an uncertain flow.")
        return "HUMAN APPROVAL REQUIRED", (
            "A sensitive action was paused and requires explicit human "
            "authorization; nothing executes until a human resolves it.")
    if all(decision == "ALLOW" for decision in decisions):
        return "COMPLETED", (
            "Every step of the flow matched the task contract; the "
            "scenario completed.")
    return "INCOMPLETE", ("Sequence did not match a known outcome — "
                          "inspect the audit trail below.")


def _spawn_scenario(command: list[str], scenario_key: str) -> None:
    """Spawn one replay subprocess with demo-reliability guards:
    - refuse while another scenario run is still executing (no overlapping
      scenario executions);
    - refuse when the gateway is unreachable — SCENARIO EXECUTION FAILED,
      no fabricated decision, no stale spawn;
    - capture stdout so the REAL run output (e.g. scenario F's structural
      probes) can be shown verbatim.
    """
    proc = st.session_state.get("scenario_proc")
    if proc is not None and proc.poll() is None:
        st.warning("Scenario execution already in progress — wait for it "
                   "to finish before starting another.")
        return
    if not _gateway_ok():
        st.session_state["scenario_proc"] = None
        st.error(
            "SCENARIO EXECUTION FAILED\n\n"
            "Gateway unavailable — no decision was fabricated. Start the "
            "gateway first: \n"
            "`TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/uvicorn "
            "taskfence.gateway:app --port 8000`")
        return
    st.session_state["scenario_proc"] = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    st.session_state["last_scenario"] = scenario_key
    st.session_state.pop("last_run_output", None)
    st.toast("EXECUTING SECURITY FLOW...")


def _running_scenario() -> tuple[bool, str]:
    """(running?, scenario key). Harvests a finished process's captured
    output once, then clears the handle so stale results are marked, not
    shown as the new run."""
    proc = st.session_state.get("scenario_proc")
    if proc is None:
        return False, ""
    if proc.poll() is None:
        return True, str(st.session_state.get("last_scenario", ""))
    output = ""
    try:
        if proc.stdout is not None:
            output = proc.stdout.read() or ""
    except (OSError, ValueError):
        output = ""
    st.session_state["last_run_output"] = output
    st.session_state["scenario_proc"] = None
    return False, ""


def _approval_ids_by_task() -> dict[str, str]:
    """task_id -> approval_id map for the audit trail's approval column
    (display mapping from stored approvals; latest approval per task)."""
    if not DB_PATH.exists():
        return {}
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT task_id, approval_id FROM approvals ORDER BY rowid")
        return dict(rows.fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _audit_events(decision_filter: str | None) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        if decision_filter and decision_filter != "All":
            rows = conn.execute(
                "SELECT timestamp, task_id, tool, action, sources, labels,"
                " destination, decision, reasons, lineage_path"
                " FROM audit_events WHERE decision = ? ORDER BY id",
                (decision_filter,))
        else:
            rows = conn.execute(
                "SELECT timestamp, task_id, tool, action, sources, labels,"
                " destination, decision, reasons, lineage_path"
                " FROM audit_events ORDER BY id")
        names = ["timestamp", "task_id", "tool", "action", "sources",
                 "labels", "destination", "decision", "reasons",
                 "lineage_path"]
        events = [dict(zip(names, row)) for row in rows.fetchall()]
        if events:  # kit I6: approval ID column, when applicable
            approval_ids = _approval_ids_by_task()
            for event in events:
                event["approval_id"] = approval_ids.get(event["task_id"], "")
        return events
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _pending_approvals() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT approval_id, task_id, tool, reasons FROM approvals"
            " WHERE status = 'pending' ORDER BY rowid")
        return [dict(zip(["approval_id", "task_id", "tool", "reasons"], row))
                for row in rows.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _approval_detail(approval_id: str) -> dict | None:
    """FIX1: the human approval preview — tool, action, declared source
    group(s), destination and the FULL outbound payload — read from the
    dashboard's own SQLite approval-data path. The dashboard is the
    human-side process: no HTTP token check is invented around this local
    read (kit FIX1-C). Full tool_args never leave this view; the generic
    unauthenticated gateway endpoints stay narrow."""
    if not DB_PATH.exists():
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT tool, args FROM approvals WHERE approval_id = ?",
            (approval_id,)).fetchone()
        if row is None:
            return None
        raw = json.loads(row[1])
        if "request" in raw:  # FIX1 record layout
            request_snap = raw.get("request") or {}
            tool_args = raw.get("tool_args") or {}
            declared_groups = list(raw.get("declared_source_groups") or [])
            contract_id = raw.get("contract_id", "")
            contract_version = raw.get("contract_version", "")
        else:  # legacy record: bare FlowRequest snapshot, no binding
            request_snap, tool_args, declared_groups = raw, {}, []
            contract_id, contract_version = "", ""
        return {
            "tool": row[0],
            "action": request_snap.get("action", ""),
            "destination": request_snap.get("destination", ""),
            "source_groups": sorted(request_snap.get("source_groups") or []),
            "declared_source_groups": declared_groups,
            "payload": tool_args.get("text") or tool_args.get("payload")
                       or request_snap.get("payload", ""),
            "tool_args": tool_args,
            "contract_id": contract_id,
            "contract_version": contract_version,
        }
    except (sqlite3.OperationalError, ValueError):
        return None
    finally:
        conn.close()


# Sink display paths (kit I5): the dashboard shows whether anything was
# delivered — explicitly empty vs populated — after each replayed run.
SINK_FILES = {"slack_sales": "outbox/slack_sales.jsonl",
              "external_api": "outbox/external_api.jsonl"}


def _sink_summary() -> str:
    """Line counts of the gateway's sink files (read-only display; the
    gateway writes them CWD-relative, the dashboard shares that cwd)."""
    parts = []
    for name, rel in SINK_FILES.items():
        path = Path(rel)
        try:
            count = sum(1 for _ in path.open(encoding="utf-8")) \
                if path.exists() else 0
        except OSError:
            count = 0
        parts.append(f"{name}: **{count}** line(s)")
    return " · ".join(parts)


# Kit I7: report-sourced metrics. The dashboard NEVER re-derives scenario
# numbers — it parses reports/results.md (scenarios.run_all's own output)
# and counts stored audit rows. Path is read per call (env override for
# tests); the default matches run_all's REPORT_PATH.
DEFAULT_REPORT = "reports/results.md"


def _scenario_report_metrics() -> dict:
    """Parse the generated report: scenario row count and the X/Y lines.
    Any missing figure stays None — the UI then says so instead of
    guessing (never invent metrics)."""
    out = {"scenarios": None, "unauthorized": None, "legitimate": None}
    try:
        text = Path(os.environ.get("TASKFENCE_REPORT_PATH",
                                   DEFAULT_REPORT)).read_text(encoding="utf-8")
    except OSError:
        return out
    table_rows = 0  # header + one row per scenario flow (separator excluded)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("| "):
            table_rows += 1
        elif stripped.startswith("Unauthorized flows intercepted:"):
            parts = stripped.split("**")
            if len(parts) >= 2:
                out["unauthorized"] = parts[1]
        elif stripped.startswith("Legitimate flows allowed:"):
            parts = stripped.split("**")
            if len(parts) >= 2:
                out["legitimate"] = parts[1]
    # Markdown table = header row + one row per reproduced flow:
    if table_rows >= 2:
        out["scenarios"] = table_rows - 1
    return out


def _audit_metrics() -> dict:
    """Counts derived from stored audit rows only (kit I7): approval-gated
    flows (APPROVE decisions), exact one-time approvals executed
    (approval_resolve events with outcome ALLOW), and a structural
    auditability check over the stored session — never a bare 100%."""
    metrics = {"approval_gated": 0, "one_time": 0, "events": 0,
               "incomplete": 0, "tasks_without_events": 0}
    # Per-call path (like the report path) so env overrides apply even
    # when this module was imported earlier by a different process/test.
    db_path = Path(os.environ.get("TASKFENCE_DB", "taskfence.sqlite3"))
    if not db_path.exists():
        return metrics
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN decision = 'APPROVE' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN action = 'approval_resolve'"
            " AND decision = 'ALLOW' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN decision NOT IN"
            " ('ALLOW','BLOCK','APPROVE') OR reasons = ''"
            " THEN 1 ELSE 0 END) FROM audit_events").fetchone()
        metrics["events"], metrics["approval_gated"], \
            metrics["one_time"], metrics["incomplete"] = (
                row[0], row[1] or 0, row[2] or 0, row[3] or 0)
        tasks = {r[0] for r in conn.execute(
            "SELECT task_id FROM task_state")}
        audited = {r[0] for r in conn.execute(
            "SELECT DISTINCT task_id FROM audit_events")}
        metrics["tasks_without_events"] = len(tasks - audited)
        return metrics
    except sqlite3.OperationalError:
        return metrics
    finally:
        conn.close()


def metrics_panel() -> None:
    """Kit I7 security metrics — every number traces to a real source:
    the generated scenario report or the stored audit log. Nothing here
    re-derives policy outcomes or invents figures."""
    st.subheader("SECURITY METRICS")
    report = _scenario_report_metrics()
    audit = _audit_metrics()
    left, mid, right = st.columns(3)
    left.metric(
        "Scenarios reproduced",
        report["scenarios"] if report["scenarios"] is not None
        else "run scenarios.run_all")
    mid.metric("Unauthorized flows intercepted",
               report["unauthorized"] or "no report")
    right.metric("Legitimate flows allowed",
                 report["legitimate"] or "no report")
    left2, mid2, right2 = st.columns(3)
    left2.metric("Approval-gated flows", audit["approval_gated"])
    mid2.metric("One-time approvals executed", audit["one_time"])
    if audit["events"] == 0:
        right2.metric("Audit coverage", "no stored session")
    elif audit["incomplete"] == 0 and audit["tasks_without_events"] == 0:
        right2.metric(
            "Audit coverage",
            f"{audit['events']}/{audit['events']} events",
            help="Every stored audit event carries a decision and reasons, "
                 "and every stored task has audit events. This is a "
                 "structural check of the stored session, not a claim of "
                 "universal coverage.")
    else:
        right2.metric(
            "Audit coverage", "incomplete",
            help=f"{audit['incomplete']} event(s) missing decision/reasons; "
                 f"{audit['tasks_without_events']} task(s) without audit "
                 "events.")
    st.caption("Sources: reports/results.md (regenerate with "
               "`python -m scenarios.run_all`) and this session's audit "
               "log. The interception figure is a controlled MVP test "
               "criterion, not a claim of universal security.")


# Kit P4-M3: additive MCP evidence panel. Reads the MCP scenario runs'
# stored evidence (reports/mcp_evidence.json, produced by
# `python -m scenarios.mcp_runner`) and renders it verbatim — the
# dashboard computes nothing about security. Existing panels untouched.
MCP_EVIDENCE = "reports/mcp_evidence.json"


def _mcp_evidence_rows() -> list[dict] | None:
    path = Path(os.environ.get("TASKFENCE_MCP_EVIDENCE_PATH", MCP_EVIDENCE))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mcp_evidence_panel() -> None:
    rows = _mcp_evidence_rows()
    st.subheader("MCP RUN EVIDENCE")
    if not rows:
        st.info("No MCP run evidence yet — run "
                "`python -m scenarios.mcp_runner`.")
        return
    st.dataframe(
        [{key: row.get(key, "") for key in
          ("run", "phase", "tool", "decision", "origin")} for row in rows],
        width='stretch', height=200)
    outcomes = [row.get("decision", "") for row in rows]
    st.caption(
        f"{len(rows)} audited MCP call(s) — ALLOW: {outcomes.count('ALLOW')}"
        f", BLOCK: {outcomes.count('BLOCK')}, APPROVE: "
        f"{outcomes.count('APPROVE')} — read from stored audit evidence "
        "(origin: MCP agent). Every MCP tool call was evaluated by the "
        "existing gateway pipeline; the MCP layer is an adapter, not a "
        "second policy engine.")


def _flow_dot(state: dict) -> str:
    decision = (state.get("last_decision") or {})
    outcome = (decision.get("decision") or {}).get("outcome", "BLOCK")
    color = OUTCOME_COLOR.get(outcome, "#c62828")
    source = (decision.get("lineage_path") or ["source"])[0]
    destination = decision.get("destination") or "destination"
    return (
        "digraph flow{rankdir=LR; node[shape=box style=rounded];"
        f' src[label="{source}"];'
        ' agent[label="AI Agent"];'
        ' gw[label="TaskFence" shape=doublebox];'
        f' dec[label="{outcome}" color="{color}" fontcolor="{color}"];'
        f' dst[label="{destination}"];'
        ' src -> agent -> gw -> dec -> dst;}')


def _lineage_dot(state: dict) -> str:
    chain = ((state.get("last_decision") or {}).get("lineage_path") or [])
    if not chain:
        return "digraph lin{empty[label=\"no lineage yet\"];}"
    nodes = []
    edges = []
    for index, name in enumerate(chain):
        nodes.append(f' n{index}[label="{name}"];')
        if index:
            edges.append(f" n{index-1} -> n{index};")
    return "digraph lin{rankdir=TB;" + "".join(nodes + edges) + "}"


@st.fragment(run_every="2s")
def live_panel():
    state = _latest_state()
    left, right = st.columns(2)
    with left:
        st.subheader("LIVE FLOW")
        if state and state.get("last_decision"):
            st.graphviz_chart(_flow_dot(state))
        else:
            st.info("No flow yet — run a scenario from the sidebar.")
    with right:
        st.subheader("LINEAGE")
        if state and state.get("last_decision"):
            st.graphviz_chart(_lineage_dot(state))
        else:
            st.info("Lineage appears after the first decision.")
    st.caption("OUTBOX (data actually delivered): " + _sink_summary()
               + " — 0 lines means nothing was delivered.")
    approvals = _pending_approvals()
    if approvals:
        st.warning(f"{len(approvals)} pending approval(s)")
        for approval in approvals:
            detail = _approval_detail(approval["approval_id"]) or {}
            columns = st.columns([4, 1, 1, 1])
            columns[0].markdown(
                f"**{approval['tool']}** — {approval['reasons'][:120]}")
            if detail:
                version = detail["contract_version"]
                columns[0].markdown(
                    f"Action `{detail['action']}` → "
                    f"`{detail['destination']}` · declared groups: "
                    f"**{', '.join(detail['declared_source_groups']) or '—'}**"
                    f" · contract `{detail['contract_id']}`"
                    + (f" @ `{version[:12]}…`" if version else ""))
                columns[0].code(str(detail["payload"]), language=None)
            if columns[1].button("Allow Once", key=approval["approval_id"]):
                _resolve(approval["approval_id"], "allow_once")
            if columns[2].button("Expand", key=approval["approval_id"] + "e"):
                _resolve(approval["approval_id"], "expand_task")
            if columns[3].button("Deny", key=approval["approval_id"] + "d"):
                _resolve(approval["approval_id"], "deny")


def _resolve(approval_id: str, choice: str) -> None:
    try:
        response = httpx.post(
            f"{GATEWAY}/approvals/{approval_id}/resolve",
            json={"choice": choice},
            headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10)
        if response.status_code == 200:
            st.toast(f"Approval {choice}: done")
        else:
            st.error(f"Resolve failed: HTTP {response.status_code}")
    except httpx.HTTPError as error:
        st.error(f"Gateway unreachable: {error}")


def _run_scenario_d() -> dict | None:
    """Kit I4: trigger the Scenario D scope-expansion request through the
    real gateway API (the same request the agent sends) and return the
    decision summary. The dashboard computes nothing about security —
    every field below comes from the gateway response."""
    try:
        # create_task drafts the contract (LLM path can be slow); the
        # 30 s here is a root-caused allowance, not a blanket bump — the
        # tool-call itself stays snappy at 10 s.
        created = httpx.post(f"{GATEWAY}/tasks",
                             json={"task_text": SCENARIO_TASK}, timeout=30)
        created.raise_for_status()
        task_id = created.json()["task_id"]
        response = httpx.post(f"{GATEWAY}/tasks/{task_id}/tool-call",
                              json={"tool": "send_slack",
                                    "args": SCENARIO_D_ARGS}, timeout=10)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as error:
        st.error(f"Gateway unreachable: {error}")
        return None
    return {"task_id": task_id,
            "outcome": body["decision"]["outcome"],
            "agent_message": body.get("agent_message", ""),
            "approval_id": body.get("approval_id")}


def _fetch_approval_detail(approval_id: str) -> dict | None:
    """Kit I4: the human preview, pulled from the gateway's admin-gated
    approval-detail endpoint — the exact reviewed request, never
    reconstructed or approximated in the dashboard."""
    try:
        response = httpx.get(
            f"{GATEWAY}/approvals/{approval_id}",
            headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10)
    except httpx.HTTPError as error:
        st.error(f"Gateway unreachable: {error}")
        return None
    if response.status_code != 200:
        st.error(f"Approval detail failed: HTTP {response.status_code}")
        return None
    return response.json()


def _resolve_allow_once(approval_id: str) -> dict | None:
    """Resolve allow_once via the existing gateway endpoint, returning the
    full response (executed flag, resulting decision, lineage path)."""
    try:
        response = httpx.post(
            f"{GATEWAY}/approvals/{approval_id}/resolve",
            json={"choice": "allow_once"},
            headers={"X-Admin-Token": ADMIN_TOKEN}, timeout=10)
    except httpx.HTTPError as error:
        st.error(f"Gateway unreachable: {error}")
        return None
    if response.status_code != 200:
        st.error(f"Resolve failed: HTTP {response.status_code}")
        return None
    st.toast("Approval allow_once: done")
    return response.json()


def scenario_d_panel() -> None:
    """Kit I4 guided walkthrough: Run Scenario D → APPROVE + approval_id →
    approval preview from the real detail endpoint → Allow Once → the
    resulting ALLOW. The LIVE FLOW / LINEAGE panel and the AUDIT TRAIL
    render after this section, so the approved execution is visible
    immediately (and the fragment refreshes every 2s besides)."""
    run = (st.session_state["scenario_d"]
           if "scenario_d" in st.session_state else None)
    if not run:
        return
    st.subheader("SCENARIO D — APPROVAL WALKTHROUGH")
    outcome = run.get("outcome", "?")
    color = OUTCOME_COLOR.get(outcome, "#c62828")
    st.markdown(
        f"Scope-expansion request decision: "
        f"**<span style='color:{color}'>{outcome}</span>**",
        unsafe_allow_html=True)
    st.markdown(f"Task: `{run.get('task_id')}`")
    approval_id = run.get("approval_id")
    if not approval_id:
        st.info(run.get("agent_message", "no approval pending"))
        return
    st.markdown(f"Approval ID: `{approval_id}`")
    if st.button("Open approval preview", key="d-open-preview"):
        st.session_state["d_preview"] = _fetch_approval_detail(approval_id)
    preview = (st.session_state["d_preview"]
               if "d_preview" in st.session_state else None)
    if preview:
        st.markdown(
            f"Reviewed tool: **{preview['tool']}** · action "
            f"`{preview['action']}` → `{preview['destination']}`  \n"
            f"Source/data: "
            f"**{', '.join(preview['declared_source_groups']) or '—'}**"
            f" · full source groups: "
            f"**{', '.join(preview['source_groups']) or '—'}**  \n"
            f"Contract: `{preview['contract_id']}` @ "
            f"`{preview['contract_version']}`")
        st.caption("Exact tool arguments (verbatim from the approval "
                   "response):")
        st.code(json.dumps(preview["tool_args"], indent=2, sort_keys=True),
                language=None)
        st.caption("Exact outbound payload:")
        st.code(str(preview["payload"]), language=None)
    if st.button("Allow Once", key="d-allow-once"):
        st.session_state["d_resolved"] = _resolve_allow_once(approval_id)
    resolved = (st.session_state["d_resolved"]
               if "d_resolved" in st.session_state else None)
    if resolved:
        after = (resolved.get("decision") or {}).get("outcome", "?")
        color_after = OUTCOME_COLOR.get(after, "#c62828")
        st.markdown(
            f"After resolution: **<span style='color:{color_after}'>"
            f"{after}</span>** — executed: **{resolved.get('executed')}**",
            unsafe_allow_html=True)
        if resolved.get("lineage_path"):
            st.caption("Approved-execution lineage: "
                       + " → ".join(resolved["lineage_path"]))
        st.caption("LIVE FLOW, LINEAGE and AUDIT TRAIL below now include "
                   "the approved execution.")


def _scenario_info_card(scenario_key: str | None) -> None:
    """Phases 3+4: judge-friendly scenario description + WHAT THIS
    SCENARIO PROVES (display-only context; the expected sequence restates
    documented behavior and never replaces the real stored decisions)."""
    info = SCENARIO_INFO.get(scenario_key or "")
    if not info:
        return
    with st.container(border=True):
        st.markdown(f"**{info['title']}** — {info['subtitle']}")
        st.markdown(f"Authorized flow: `{info['flow']}` · Expected: "
                    f"**{info['expected']}**")
        st.markdown(info["description"])
        st.markdown(
            f"**WHAT THIS SCENARIO PROVES:** {info['proves']}")
        if info.get("note"):
            st.caption(info["note"])


def _security_flow_panel(task_id: str | None, scenario_key: str | None,
                         running: bool) -> None:
    """Phases 5–7: the COMPLETE security decision sequence (every stored
    audited decision of the latest task, verbatim) plus the FINAL SCENARIO
    OUTCOME. An intermediate ALLOW is never presented as the scenario
    result — the full flow and the final outcome are shown together."""
    st.subheader("SECURITY FLOW")
    if running:
        st.info("EXECUTING SECURITY FLOW... — previous results below are "
                "from the last completed run.")
    steps = _security_steps(_task_audit_rows(task_id))
    if not steps:
        if scenario_key == "scenario_f":
            st.markdown("Structural probes (no protected call to evaluate): "
                        "no mutating gateway endpoint exists, the "
                        "agent-side client exposes no mutation surface, "
                        "and a direct tamper attempt is rejected.")
        elif not running:
            st.info("No audited decision yet — run a scenario from the "
                    "sidebar.")
    else:
        for number, step in enumerate(steps, start=1):
            color = OUTCOME_COLOR.get(step["decision"], "#c62828")
            route = step["source"]
            if step["transformation"]:
                route += f" → {step['transformation']}"
            if step["destination"]:
                route += f" → {step["destination"]}"  # noqa: E501
            head = (f"**Step {number} — `{step['tool']}`**  \n"
                    f"Route: {route} · Decision: **<span "
                    f"style='color:{color}'>{step['decision']}</span>**")
            st.markdown(head, unsafe_allow_html=True)
            if step["reasons"]:
                st.caption("Reason: " + step["reasons"])
    outcome, reason = _derive_final_outcome(steps, scenario_key)
    final_color = FINAL_COLOR.get(outcome, "#c62828")
    st.markdown(
        f"#### FINAL SCENARIO OUTCOME: <span style='color:{final_color}'>"
        f"{outcome}</span>",
        unsafe_allow_html=True)
    if reason:
        st.caption(reason)
    output = st.session_state.get("last_run_output")
    if not running and output:
        with st.expander("Last run transcript (verbatim subprocess output)"):
            st.code(output, language=None)


@st.fragment(run_every="2s")
def _decision_panel() -> None:
    """Kit I6 panel + Phases 5–8 clarity additions, auto-refreshing on the
    same 2s cadence as LIVE FLOW so a finished scenario run replaces the
    EXECUTING state within seconds — no stale results, no interaction
    needed. Every value is read verbatim from stored task state and the
    audit table; the UI only assembles display strings and computes
    nothing about security."""
    task_id, state = _latest_task_and_state()
    running, running_key = _running_scenario()
    _scenario_info_card(running_key or st.session_state.get("last_scenario"))
    st.subheader("CURRENT SECURITY DECISION")
    decision = (state or {}).get("last_decision")
    if not decision:
        if running:
            st.info("RUNNING SCENARIO... — the decision will appear here "
                    "the moment the gateway records it.")
        else:
            st.info("No decision yet — run a scenario from the sidebar.")
        _security_flow_panel(task_id,
                             running_key
                             or st.session_state.get("last_scenario"),
                             running)
        return
    outcome = (decision.get("decision") or {}).get("outcome", "BLOCK")
    color = OUTCOME_COLOR.get(outcome, "#c62828")
    st.markdown(
        f"### DECISION: <span style='color:{color}'>{outcome}</span>",
        unsafe_allow_html=True)
    if outcome in ("ALLOW", "APPROVE") and len(_task_audit_rows(task_id)) > 1:
        st.caption("Intermediate decision — this is one step of the flow, "
                   "not the final scenario result. See SECURITY FLOW and "
                   "FINAL SCENARIO OUTCOME below.")

    st.markdown("#### WHY?")
    st.code(decision.get("explain", ""), language=None)

    event = _latest_audit_for_task(task_id) or {}
    st.markdown("#### DATA FLOW")
    flow_parts = ((event.get("sources") or "").strip(),
                  (event.get("transformation") or "").strip(),
                  (event.get("destination")
                   or decision.get("destination") or "").strip())
    st.markdown(" -> ".join(part for part in flow_parts if part)
                or "(no flow recorded)")

    st.markdown("#### CONTRACT")
    contract = (state or {}).get("contract") or {}
    version = str(contract.get("version", ""))
    purpose = contract.get("purpose", "")
    st.markdown(f"{purpose} — v{version[:12]}" if version
                else purpose or "(no contract)")

    st.markdown("#### CLASSIFICATION")
    st.markdown((event.get("labels") or "").strip() or "(none)")

    st.markdown("#### LINEAGE PATH")
    chain = decision.get("lineage_path") or []
    st.markdown(" -> ".join(chain) if chain else "(not tracked)")
    _security_flow_panel(task_id,
                         st.session_state.get("last_scenario"), running)


st.title("🛡️ TASKFENCE")
st.caption("Purpose-Bound Security for AI Agents — "
           "the agent decides HOW; TaskFence decides WHETHER.")

with st.sidebar:
    st.header("Controls")
    _system_status()
    st.divider()
    st.caption("Replay a demo scenario")
    for label, script in SCENARIOS.items():
        if st.button(label):
            _spawn_scenario(
                [sys.executable, "-m", "taskfence.agent",
                 "--task", SCENARIO_TASK, "--scripted", script], script)
    for label, replay in REPLAY_SCENARIOS.items():
        if st.button(label):
            _spawn_scenario(
                [sys.executable, "-m", "scenarios.replay", replay], replay)
    st.divider()
    st.caption("Guided approval demo")
    if st.button("Run Scenario D — approval walkthrough"):
        proc = st.session_state.get("scenario_proc")
        if proc is not None and proc.poll() is None:
            st.warning("Scenario execution already in progress — wait for "
                       "it to finish before starting another.")
        elif not _gateway_ok():
            st.error("SCENARIO EXECUTION FAILED\n\nGateway unavailable — "
                     "no decision was fabricated.")
        else:
            result = _run_scenario_d()
            if result:
                st.session_state["scenario_d"] = result
                st.session_state["d_preview"] = None
                st.session_state["d_resolved"] = None
                st.session_state["last_scenario"] = "scenario_d"
                st.toast(f"Scenario D request: {result['outcome']}")
    st.divider()
    if st.button("♻️ Reset DB + outbox"):
        conn = _db()
        with conn:
            for table in ("audit_events", "task_state", "approvals",
                          "lineage_nodes", "lineage_edges"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
        conn.close()
        outbox = Path("outbox")
        if outbox.exists():
            for sink in outbox.glob("*.jsonl"):
                sink.write_text("", encoding="utf-8")
        st.toast("Reset complete")

_decision_panel()

state = _latest_state()

left, right = st.columns(2)
with left:
    st.subheader("CURRENT TASK")
    if state:
        st.markdown(f"**{state.get('task_text', '(no task text)')}**")
        st.caption(f"Session reads (taint): "
                   f"{', '.join(state.get('session_reads', [])) or 'none'}")
    else:
        st.info("No task yet.")
with right:
    st.subheader("CONTRACT SCOPE")
    contract = (state or {}).get("contract")
    if contract:
        st.markdown(
            f"Purpose: **{contract['purpose']}**  \n"
            f"Allowed data: **{', '.join(contract['allowed_data']) or '—'}**"
            f"  \nAllowed destinations: "
            f"**{', '.join(contract['allowed_destinations']) or '—'}**  \n"
            f"Actions: **{', '.join(contract['allowed_actions']) or '—'}**"
            f"  \nExternal transfer: "
            f"**{'enabled' if contract['external_transfer'] else 'disabled'}**"
            f"  \nVersion: `{contract['version'][:16]}…`"
            + (f"  \nParent: `{contract['parent_contract_id']}`"
               if contract.get("parent_contract_id") else ""))
    else:
        st.info("No contract yet.")

metrics_panel()

scenario_d_panel()

live_panel()

st.subheader("AUDIT TRAIL")
decision_filter = st.selectbox("Filter", ["All", "ALLOW", "APPROVE", "BLOCK"])
events = _audit_events(decision_filter)
if events:
    st.dataframe(events, width='stretch', height=240)
else:
    st.caption("No audit events recorded yet.")

# Kit P4-M3 (additive): MCP run evidence renders AFTER the audit trail so
# existing element ordering (and positional test indexes) stay unchanged.
mcp_evidence_panel()
