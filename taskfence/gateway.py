"""TaskFence gateway (spec §6, §14, §27; DECISIONS §3, §7; kit Prompt C2).

Pipeline for every tool call:
  resolve sources -> classify (incl. outbound rescan) -> lineage update ->
  policy -> audit -> execute the tool ONLY on ALLOW.

Invariants honored here: I1 (tools are private to this module), I4 (unknown
tool/asset => BLOCK + audit), I5 (every decision audits), I6/I7 (labels and
lineage per DECISIONS §4; contracts come only from POST /tasks task text).

DECISIONS §3: with GATE_OUT_OF_SCOPE_READS=False (default), an out-of-scope
read is ALLOWed but tainted (session reads) and audited; with True it stays
APPROVE for human review.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from taskfence import registry, tools
from taskfence.audit import AuditLog
from taskfence.classifier import classify
from taskfence.explain import explain
from taskfence.lineage import LineageTracker
from taskfence.models import FlowRequest, TaskContract
from taskfence.policy import PolicyEngine

GATE_OUT_OF_SCOPE_READS = False  # DECISIONS §3 (kit Prompt C2 flag)

AGENT_MESSAGE_BLOCK = ("This request was denied by security policy. "
                       "A human has been notified.")
AGENT_MESSAGE_APPROVE = ("This request needs human approval before it can "
                         "proceed.")
AGENT_MESSAGE_ALLOW = "Completed."

app = FastAPI(title="TaskFence Gateway")
ENGINE = PolicyEngine()
TRACKER = LineageTracker()
AUDIT = AuditLog()

CONTRACTS: dict[str, TaskContract] = {}
TASK_TEXT: dict[str, str] = {}
LAST_DECISION: dict[str, dict] = {}


class TaskIn(BaseModel):
    task_text: str


class ToolCallIn(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)


def build_sales_reporting_contract(task_text: str) -> TaskContract:
    """Contract builder entry point (kit Prompt D1 wires the real builder
    in here; the deterministic keyword fallback keeps the demo alive when
    Ollama is down — DECISIONS §8)."""
    from taskfence.contract import build_contract
    return build_contract(task_text)
    return TaskContract(
        contract_id="c-sales-stub",
        purpose="sales_reporting",
        allowed_data=["sales_reports"],
        allowed_destinations=["sales_slack"],
        allowed_actions=["read", "summarize", "send_message"],
        external_transfer=False,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_asset(args: dict) -> str | None:
    """Map agent args to a registered asset id, or None (default deny)."""
    asset_id = args.get("asset_id")
    if asset_id in registry.ASSETS:
        return asset_id
    path = str(args.get("path", ""))
    for candidate, entry in registry.ASSETS.items():
        if Path(entry[0]).name == Path(path).name:
            return candidate
    return None


def _audit(task_id: str, request: FlowRequest, decision, lineage: list[str],
           contract: TaskContract | None = None) -> None:
    AUDIT.log_event({
        "task_id": task_id,
        "task_text": TASK_TEXT.get(task_id, ""),
        "contract_id": contract.contract_id if contract else "",
        "contract_version": decision.contract_version,
        "tool": request.tool, "action": request.action,
        "sources": [request.source], "labels": sorted(request.labels),
        "transformation": request.transformation,
        "destination": request.destination, "decision": decision.outcome,
        "reasons": decision.reasons,
        "lineage_path": " -> ".join(lineage)})


def _session_groups(task_id: str) -> set[str]:
    groups = set()
    for asset_id in TRACKER.session_reads.get(task_id, set()):
        info = registry.resolve(asset_id)
        if info:
            groups.add(info["data_group"])
    return groups


def _handle_read(task_id: str, contract, tool: str, meta: dict, args: dict):
    asset_id = ("customer_db" if tool == "query_customer_db"
                else _resolve_asset(args))
    if asset_id is None:
        request = FlowRequest(tool=tool, action=meta["action"],
                              source=str(args.get("path", "")),
                              destination="")
        return _finish(task_id, contract, request, [], None)
    content = registry.read_asset(asset_id)
    labels = classify(asset_id, content)
    TRACKER.register_asset(task_id, asset_id)
    request = FlowRequest(
        tool=tool, action=meta["action"], source=asset_id,
        source_group=registry.resolve(asset_id)["data_group"],
        destination="", labels=frozenset(labels), transformation="",
        payload="")
    decision = ENGINE.evaluate(contract, request)
    tainted = False
    if (decision.outcome == "APPROVE" and not GATE_OUT_OF_SCOPE_READS
            and meta["action"] in ("read", "query")):
        # DECISIONS §3 Option 1: allow-but-taint (session tainted, audited,
        # executed). With GATE_OUT_OF_SCOPE_READS=True the APPROVE stands.
        tainted = True
        decision = decision.model_copy(update={
            "outcome": "ALLOW",
            "reasons": ["Out-of-scope read allowed under allow-but-taint; "
                        "session tainted and audited (DECISIONS §3)."],
            "failed_checks": decision.failed_checks})
        result = tools.TOOL_FUNCTIONS[tool](
            {"task_id": task_id, "source_asset": asset_id,
             "content": content}, **args)
        return _finish(task_id, contract, request, [asset_id], result,
                       decision, tainted=tainted)
    result = None
    if decision.outcome == "ALLOW":
        result = tools.TOOL_FUNCTIONS[tool](
            {"task_id": task_id, "source_asset": asset_id,
             "content": content}, **args)
    return _finish(task_id, contract, request, [asset_id], result,
                   decision)


def _handle_outbound(task_id: str, contract, tool: str, meta: dict,
                     args: dict):
    content = str(args.get("text") or args.get("payload") or "")
    declared = list(args.get("source_assets", []))
    groups: set[str] = set()
    labels: set[str] = classify(None, content)  # outbound rescan (kit C-T)
    for node_id in declared:
        info = registry.resolve(node_id)
        if info:
            groups.add(info["data_group"])
            labels |= registry.base_labels(node_id)
        labels |= TRACKER.effective_labels(task_id, node_id)
    if not declared:
        groups = _session_groups(task_id)  # conservative taint (DECISIONS §4)
    for group in _session_groups(task_id):
        if TRACKER.mode == "conservative":
            groups.add(group)
    destination = meta["destination"]
    payload_node = str(args.get("payload_node") or
                       (declared[0] if declared else "outbound_payload"))
    request = FlowRequest(
        tool=tool, action=meta["action"],
        source=(declared[0] if declared else "session_reads"),
        source_group=(next(iter(groups)) if groups else ""),
        source_groups=frozenset(groups),
        destination=destination, labels=frozenset(labels),
        transformation=str(args.get("transformation", "compose")),
        payload=content[:200])
    decision = ENGINE.evaluate(contract, request)
    result = None
    if decision.outcome == "ALLOW":
        result = tools.TOOL_FUNCTIONS[tool](
            {"task_id": task_id, "source_asset": None,
             "content": content}, **args)
        TRACKER.record_sink(task_id, payload_node, destination)
    return _finish(task_id, contract, request,
                   TRACKER.path_to(task_id, payload_node), result,
                   decision)


def _finish(task_id: str, contract, request: FlowRequest, lineage: list[str],
            result, decision=None, tainted: bool = False) -> dict:
    if decision is None:
        from taskfence.policy import HARD_UNKNOWN_ENTITY
        decision = ENGINE.evaluate(contract, request)
    _audit(task_id, request, decision, lineage, contract)
    LAST_DECISION[task_id] = {
        "decision": decision.model_dump(),
        "explain": explain(decision, request, contract, lineage),
        "lineage_path": lineage, "tool": request.tool,
        "destination": request.destination}
    agent_message = {"ALLOW": AGENT_MESSAGE_ALLOW,
                     "APPROVE": AGENT_MESSAGE_APPROVE,
                     "BLOCK": AGENT_MESSAGE_BLOCK}[decision.outcome]
    return {"decision": decision.model_dump(), "agent_message": agent_message,
            "explain": LAST_DECISION[task_id]["explain"],
            "lineage_path": lineage, "tainted": tainted, "result": result}


@app.post("/tasks")
def create_task(body: TaskIn):
    task_id = f"task-{datetime.now(timezone.utc).timestamp():.0f}"
    contract = build_sales_reporting_contract(body.task_text)
    CONTRACTS[task_id] = contract
    TASK_TEXT[task_id] = body.task_text
    return {"task_id": task_id, "contract": contract.model_dump()}


@app.post("/tasks/{task_id}/tool-call")
def tool_call(task_id: str, body: ToolCallIn):
    contract = CONTRACTS.get(task_id)
    if contract is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="unknown task")
    meta = tools.TOOL_REGISTRY.get(body.tool)
    if meta is None:  # I4: unknown tool => BLOCK + audit, never execute
        request = FlowRequest(tool=body.tool, action="read", source="",
                              destination="")
        return _finish(task_id, contract, request, [], None)
    if meta["action"] in ("read", "query"):
        return _handle_read(task_id, contract, body.tool, meta, body.args)
    return _handle_outbound(task_id, contract, body.tool, meta, body.args)


@app.get("/tasks/{task_id}")
def task_state(task_id: str):
    if task_id not in CONTRACTS:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="unknown task")
    return {"task_id": task_id, "task_text": TASK_TEXT[task_id],
            "contract": CONTRACTS[task_id].model_dump(),
            "last_decision": LAST_DECISION.get(task_id),
            "session_reads": sorted(
                TRACKER.session_reads.get(task_id, set()))}


@app.get("/audit")
def read_audit(task_id: str | None = None):
    return {"events": AUDIT.read_all(task_id)}


@app.get("/lineage/{task_id}")
def read_lineage(task_id: str):
    nodes = [{"node": node, "labels": sorted(data.get("labels", set()))}
             for (tid, node), data in TRACKER._graph.nodes(data=True)
             if tid == task_id]
    edges = [{"src": src, "dst": dst,
              "transformation": data.get("transformation", "")}
             for (tid, src), (tid2, dst), data in TRACKER._graph.edges(
                 data=True) if tid == task_id and tid2 == task_id]
    return {"task_id": task_id, "nodes": nodes, "edges": edges}
