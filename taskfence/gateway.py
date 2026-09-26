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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from taskfence import catalog, registry, tools
from taskfence.approvals import ApprovalStore
from taskfence.audit import AuditLog
from taskfence.classifier import classify
from taskfence.explain import explain
from taskfence.lineage import LineageTracker
from taskfence.models import FlowRequest, TaskContract
from taskfence.policy import PolicyEngine
from typing import Literal

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
APPROVALS = ApprovalStore()

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


def _audit_event(task_id: str, event: dict) -> None:
    """Append a raw audit event (approval workflow, contract versions)."""
    AUDIT.log_event(event)


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
    approval_id = None
    if decision.outcome == "APPROVE":
        request_data = request.model_dump()
        request_data["labels"] = sorted(request_data["labels"])
        request_data["source_groups"] = sorted(request_data["source_groups"])
        approval_id = APPROVALS.create(task_id, request.tool, request_data,
                                       decision.reasons)
    return {"decision": decision.model_dump(), "agent_message": agent_message,
            "explain": LAST_DECISION[task_id]["explain"],
            "lineage_path": lineage, "tainted": tainted, "result": result,
            "approval_id": approval_id}


@app.post("/tasks")
def create_task(body: TaskIn):
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    contract = build_sales_reporting_contract(body.task_text)
    CONTRACTS[task_id] = contract
    TASK_TEXT[task_id] = body.task_text
    return {"task_id": task_id, "contract": contract.model_dump()}


@app.post("/tasks/{task_id}/tool-call")
def tool_call(task_id: str, body: ToolCallIn):
    contract = CONTRACTS.get(task_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="unknown task")
    meta = tools.TOOL_REGISTRY.get(body.tool)
    if meta is None:  # I4: unknown tool => BLOCK + audit, never execute
        request = FlowRequest(tool=body.tool, action="read", source="",
                              destination="")
        return _finish(task_id, contract, request, [], None)
    if meta["action"] in ("read", "query"):
        return _handle_read(task_id, contract, body.tool, meta, body.args)
    return _handle_outbound(task_id, contract, body.tool, meta, body.args)


def _require_admin(request: Request) -> None:
    """Human-only resolution: the agent-side GatewayClient never holds this
    token (DECISIONS §9)."""
    expected = os.environ.get("TASKFENCE_ADMIN_TOKEN")
    if not expected or request.headers.get("X-Admin-Token") != expected:
        raise HTTPException(status_code=403,
                            detail="admin token required")


class ResolveIn(BaseModel):
    choice: Literal["allow_once", "expand_task", "deny"]


@app.post("/approvals/{approval_id}/resolve")
def resolve_approval(approval_id: str, body: ResolveIn,
                     request: Request):
    _require_admin(request)
    record = APPROVALS.get(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown approval")
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="already resolved")
    task_id = record["task_id"]
    contract = CONTRACTS.get(task_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="unknown task")

    if body.choice == "deny":
        APPROVALS.set_status(approval_id, "denied")
        _audit_event(task_id, {
            "task_id": task_id, "task_text": TASK_TEXT.get(task_id, ""),
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "tool": record["tool"], "action": "approval_resolve",
            "sources": [], "labels": [], "transformation": "",
            "destination": "", "decision": "BLOCK",
            "reasons": ["Human denied the approval request."],
            "lineage_path": ""})
        return {"status": "denied", "executed": False}

    if body.choice == "allow_once":
        # Single-use: executes this stored request exactly once, with the
        # exact contract that produced it. No contract change (DECISIONS §9).
        APPROVALS.set_status(approval_id, "allowed_once")
        stored = FlowRequest.model_validate(record["args"])
        decision = ENGINE.evaluate(contract, stored)
        if decision.outcome != "ALLOW":
            decision = decision.model_copy(update={
                "outcome": "ALLOW",
                "reasons": decision.reasons + [
                    "Human approved this single request."],
                "failed_checks": decision.failed_checks})
        result = None
        meta = tools.TOOL_REGISTRY.get(record["tool"], {})
        if meta and meta.get("action") in ("read", "query"):
            asset_id = stored.source or None
            content = registry.read_asset(asset_id) if asset_id else ""
            result = tools.TOOL_FUNCTIONS[record["tool"]](
                {"task_id": task_id, "source_asset": asset_id,
                 "content": content}, **(record["args"].get("args")
                                         if isinstance(
                                             record["args"].get("args"),
                                             dict) else {}))
        elif meta:
            content = str(record["args"].get("payload")
                          or record["args"].get("text") or "")
            result = tools.TOOL_FUNCTIONS[record["tool"]](
                {"task_id": task_id, "source_asset": None,
                 "content": content}, **(record["args"].get("args")
                                         if isinstance(
                                             record["args"].get("args"),
                                             dict) else {}))
        _audit_event(task_id, {
            "task_id": task_id, "task_text": TASK_TEXT.get(task_id, ""),
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "tool": record["tool"], "action": "approval_resolve",
            "sources": [stored.source], "labels": sorted(stored.labels),
            "transformation": "allow_once",
            "destination": stored.destination, "decision": "ALLOW",
            "reasons": ["Human approved this single request."],
            "lineage_path": ""})
        return {"status": "allowed_once", "executed": True,
                "decision": decision.model_dump(), "result": result}

    # expand_task: NEW contract version (I3: the old one is never mutated).
    APPROVALS.set_status(approval_id, "expanded")
    stored = FlowRequest.model_validate(record["args"])
    new_allowed_data = sorted(set(contract.allowed_data)
                              | {g for g in stored.source_groups
                                 | {stored.source_group}
                                 if g in catalog.DATA_GROUPS})
    new_actions = sorted(set(contract.allowed_actions) | {stored.action})
    new_contract = TaskContract(
        contract_id=contract.contract_id + "-x",
        parent_contract_id=contract.contract_id,
        purpose=contract.purpose,
        allowed_data=new_allowed_data,
        allowed_destinations=list(contract.allowed_destinations),
        allowed_actions=new_actions,
        external_transfer=contract.external_transfer,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    CONTRACTS[task_id] = new_contract
    _audit_event(task_id, {
        "task_id": task_id, "task_text": TASK_TEXT.get(task_id, ""),
        "contract_id": new_contract.contract_id,
        "contract_version": new_contract.version,
        "tool": record["tool"], "action": "approval_resolve",
        "sources": [stored.source], "labels": sorted(stored.labels),
        "transformation": "expand_task",
        "destination": stored.destination, "decision": "ALLOW",
        "reasons": [f"Task scope expanded to contract version "
                    f"{new_contract.version[:12]}…; parent "
                    f"{contract.contract_id} unchanged."],
        "lineage_path": ""})
    return {"status": "expanded",
            "contract": new_contract.model_dump()}


@app.get("/approvals")
def list_approvals(task_id: str | None = None):
    return {"pending": APPROVALS.pending(task_id)}


@app.get("/tasks/{task_id}")
def task_state(task_id: str):
    if task_id not in CONTRACTS:
        raise HTTPException(status_code=404, detail="unknown task")
    return {"task_id": task_id, "task_text": TASK_TEXT[task_id],
            "contract": CONTRACTS[task_id].model_dump(),
            "last_decision": LAST_DECISION.get(task_id),
            "session_reads": sorted(
                TRACKER.session_reads.get(task_id, set())),
            "pending_approvals": APPROVALS.pending(task_id)}


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
