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
        created = httpx.post(f"{GATEWAY}/tasks",
                             json={"task_text": SCENARIO_TASK}, timeout=10)
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


def _decision_panel() -> None:
    """Kit I6: the six-field decision panel — Decision, Why?, Data Flow,
    Contract, Classification, Lineage. Every value is read verbatim from
    stored task state and the audit table; the UI only assembles display
    strings and computes nothing about security."""
    task_id, state = _latest_task_and_state()
    st.subheader("CURRENT DECISION")
    decision = (state or {}).get("last_decision")
    if not decision:
        st.info("No decision yet — run a scenario from the sidebar.")
        return
    outcome = (decision.get("decision") or {}).get("outcome", "BLOCK")
    color = OUTCOME_COLOR.get(outcome, "#c62828")
    st.markdown(
        f"### DECISION: <span style='color:{color}'>{outcome}</span>",
        unsafe_allow_html=True)

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


st.title("🛡️ TASKFENCE")
st.caption("Purpose-Bound Security for AI Agents — "
           "the agent decides HOW; TaskFence decides WHETHER.")

with st.sidebar:
    st.header("Controls")
    st.caption("Replay a demo scenario")
    for label, script in SCENARIOS.items():
        if st.button(label):
            subprocess.Popen(
                [sys.executable, "-m", "taskfence.agent",
                 "--task", SCENARIO_TASK, "--scripted", script])
            st.toast(f"Started {script}")
    for label, replay in REPLAY_SCENARIOS.items():
        if st.button(label):
            subprocess.Popen(
                [sys.executable, "-m", "scenarios.replay", replay])
            st.toast(f"Started {replay}")
    st.divider()
    st.caption("Guided approval demo")
    if st.button("Run Scenario D — approval walkthrough"):
        result = _run_scenario_d()
        if result:
            st.session_state["scenario_d"] = result
            st.session_state["d_preview"] = None
            st.session_state["d_resolved"] = None
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
