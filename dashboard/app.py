"""TaskFence dashboard (spec §17, §29, §31; kit Prompts F1, F2, F3).

Reads SQLite only — it is a separate process from the gateway. The UI
computes NOTHING about security: every decision, reason and lineage chain
is rendered from what the gateway persisted. Sidebar buttons are triggers
only (scenario runs, approval resolution via the gateway API, reset).
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
SCENARIO_TASK = "Summarize Q3 sales and post it to #sales."


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


def _latest_state() -> dict | None:
    if not DB_PATH.exists():
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT state FROM task_state ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None
    except sqlite3.OperationalError:
        return None
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
        return [dict(zip(names, row)) for row in rows.fetchall()]
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
    decision = ((state or {}).get("last_decision") or {})
    if decision:
        outcome = decision["decision"]["outcome"]
        color = OUTCOME_COLOR.get(outcome, "#c62828")
        st.markdown(
            f"### DECISION: <span style='color:{color}'>{outcome}</span>",
            unsafe_allow_html=True)
        st.code(decision.get("explain", ""), language=None)
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


st.title("🛡️ TASKFENCE")
st.caption("Purpose-Bound Security for AI Agents — "
           "the agent decides HOW; TaskFence decides WHETHER.")

with st.sidebar:
    st.header("Controls")
    st.subheader("Run a scenario")
    for label, script in SCENARIOS.items():
        if st.button(label):
            subprocess.Popen(
                [sys.executable, "-m", "taskfence.agent",
                 "--task", SCENARIO_TASK, "--scripted", script])
            st.toast(f"Started {script}")
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
    st.subheader("TASK CONTRACT")
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

live_panel()

st.subheader("AUDIT TRAIL")
decision_filter = st.selectbox("Filter", ["All", "ALLOW", "APPROVE", "BLOCK"])
events = _audit_events(decision_filter)
if events:
    st.dataframe(events, width='stretch', height=240)
else:
    st.caption("No audit events recorded yet.")
