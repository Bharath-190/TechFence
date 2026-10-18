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

FIX1 (approval integrity): approval records store the EXACT original tool
args plus a binding to the reviewed contract (id + version + frozen
snapshot). allow_once replays the exact request under that binding with the
normal ALLOW bookkeeping; resolution fails closed on any missing/stale
binding BEFORE the approval is claimed or the tool runs.
"""

import os
import uuid
from datetime import datetime, timezone
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

import hashlib
import json

from taskfence import catalog, registry, tools
from taskfence.approvals import ApprovalStore
from taskfence.audit import AuditLog, TaskStateStore
from taskfence.auth import JWTRejected, JWTUnavailable, JWTVerifier
from taskfence.classifier import classify
from taskfence.config import load_config
from taskfence.explain import explain
from taskfence.lineage import LineageTracker
from taskfence.models import FlowRequest, TaskContract
from taskfence.persistence import Persistence
from taskfence.policy import PolicyEngine
from typing import Literal


GATE_OUT_OF_SCOPE_READS = False  # DECISIONS §3 (kit Prompt C2 flag)

AGENT_MESSAGE_BLOCK = ("This request was denied by security policy. "
                       "A human has been notified.")
AGENT_MESSAGE_APPROVE = ("This request needs human approval before it can "
                         "proceed.")
AGENT_MESSAGE_ALLOW = "Completed."

app = FastAPI(title="TaskFence Gateway")
CONFIG = load_config()
# Phase 3 (G-04): the JWT verifier is built ONCE from the validated startup
# configuration and is fixed for the process lifetime. `None` means JWT mode
# is disabled and the legacy X-Admin-Token behaviour applies unchanged. A
# bad enabled-mode configuration raises ConfigError here, so the process
# never starts in a half-authenticated state and can never silently
# downgrade to legacy authentication.
JWT = JWTVerifier.from_config(CONFIG)
# One configured backend for every store (AD-21/AD-25, G-16). No module
# below may open its own database file.
PERSISTENCE = Persistence(CONFIG)
DB = PERSISTENCE.db
ENGINE = PolicyEngine()
TASKS = PERSISTENCE.tasks
TRACKER = PERSISTENCE.lineage
AUDIT = PERSISTENCE.audit
APPROVALS = PERSISTENCE.approvals
STATE = PERSISTENCE.state

# Expansion liveness (I3). The durable task row is the AUTHORITY for the
# contract, but `expand_task` issues a NEW contract version for the review
# session that produced the approval. A live session means "this process
# created the task"; it is process state by nature and deliberately NOT
# persisted, so after a restart (or in another worker) there is no live
# session to expand and the approval stays pending for deny / allow_once.
# F2: a pending expansion approval can never widen scope indefinitely, and
# no expiry policy is invented here — Phase 10 owns retention.
# Bounded so a long-lived process cannot grow without limit; an evicted id
# can only cause a fail-closed 409 for an expansion, never a grant.
LIVE_TASK_LIMIT = 10_000
LIVE_TASKS: "OrderedDict[str, None]" = OrderedDict()


def _mark_live(task_id: str) -> None:
    """Record that this process is serving a review session for the task."""
    LIVE_TASKS[task_id] = None
    LIVE_TASKS.move_to_end(task_id)
    while len(LIVE_TASKS) > LIVE_TASK_LIMIT:
        LIVE_TASKS.popitem(last=False)


def _session_is_live(task_id: str) -> bool:
    """True only if THIS process created the task (never after a restart)."""
    return task_id in LIVE_TASKS


def _fingerprint(tool: str, args: dict) -> str:
    """Stable request identity for the append-only decision history."""
    payload = json.dumps({"tool": tool, "args": args},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _connector_fingerprint(meta: dict) -> str:
    """Digest of the effective delivery configuration (never the secret).

    A tool that can deliver for real (``real_delivery``) fingerprints which
    connector is configured: no connector at all, or a digest of the
    configured webhook URL. The URL itself is never stored, logged or
    audited — the digest only exists so a replay can detect that the
    delivery target changed after the human reviewed the request (R1b).
    """
    if not meta.get("real_delivery"):
        return "none"
    webhook = tools.real_slack_webhook()
    if not webhook:
        return "fake_sink"
    return "real:" + _sha256(webhook)[:32]


SIDE_EFFECT_CLASSIFICATIONS = {
    "read_only",
    "external_delivery",
    "internal_sink",
}


def _tool_binding(tool: str) -> dict | None:
    """What the reviewer saw about the tool: identity, action, destination
    and the connector configuration in force at review time (R1b).

    Also validates that the tool's side_effect_classification is present
    and recognized. An unclassified tool fails closed.
    """
    meta = tools.TOOL_REGISTRY.get(tool)
    if meta is None:
        return None
    classification = meta.get("side_effect_classification")
    if classification not in SIDE_EFFECT_CLASSIFICATIONS:
        raise ValueError(
            f"unrecognized side_effect_classification '{classification}' "
            f"for tool '{tool}'")
    return {"tool": tool, "action": meta.get("action", ""),
            "destination": meta.get("destination", ""),
            "connector": _connector_fingerprint(meta),
            "side_effect_classification": classification}


def _asset_binding(asset_id: str, content: str, labels) -> dict:
    """Content digest + classification of a read asset at review time.

    An approved read is bound to the bytes and labels the human reviewed, so
    a replay cannot return content that changed underneath the approval
    (R3).
    """
    return {"asset_id": asset_id, "content_sha256": _sha256(content),
            "labels": sorted(set(labels))}


class TaskIn(BaseModel):
    task_text: str


class ToolCallIn(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)


def build_task_contract(task_text: str) -> TaskContract:
    """Contract builder entry point (kit Prompt D1 wires the real builder
    in here; the deterministic keyword fallback keeps the demo alive when
    Ollama is down — DECISIONS §8)."""
    from taskfence.contract import build_contract
    return build_contract(task_text)


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
        "task_text": TASKS.task_text(task_id),
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
    for asset_id in TRACKER.session_asset_ids(task_id):
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
        return _finish(task_id, contract, request, [], None,
                       tool_args=args, declared=[])
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
                       decision, tainted=tainted, tool_args=args,
                       declared=[asset_id],
                       asset_binding=_asset_binding(asset_id, content, labels))
    result = None
    if decision.outcome == "ALLOW":
        result = tools.TOOL_FUNCTIONS[tool](
            {"task_id": task_id, "source_asset": asset_id,
             "content": content}, **args)
    return _finish(task_id, contract, request, [asset_id], result,
                   decision, tool_args=args, declared=[asset_id],
                   asset_binding=_asset_binding(asset_id, content, labels))


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
        source_group=(sorted(groups)[0] if groups else ""),
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
                   decision, tool_args=args, declared=declared)


def _stored_task_text(task_id: str) -> str:
    """Task text for audit/state DISPLAY: the live map first, then the
    stored audit trail (which survives gateway restarts). Display data
    only — never a security input; decisions come from the frozen
    approval binding, never from reconstructed task text (FIX1)."""
    text = TASKS.task_text(task_id)
    if text:
        return text
    events = AUDIT.read_all(task_id)
    return events[-1].get("task_text", "") if events else ""


def _decision_view(task_id: str) -> dict | None:
    """Display-shaped view of the newest durable decision row.

    Derived from `task_decisions` (append-only authority) purely for
    rendering; nothing in an authorization path reads this shape.
    """
    row = TASKS.latest_decision(task_id)
    if row is None:
        return None
    return {
        "decision": {"outcome": row["outcome"], "reasons": row["reasons"],
                     "failed_checks": [],
                     "contract_version": row["contract_version"]},
        "explain": "", "lineage_path": [], "tool": row["tool"],
        "destination": row["destination"]}


def _save_state(task_id: str, contract=None, last_decision=None) -> None:
    """Persist live task state for the dashboard process (display only).

    `contract` defaults to the task's live contract. Resolving a PERSISTED
    approval after a gateway restart passes the REVIEWED frozen contract
    explicitly — the exact contract the human reviewed — so the stored
    state still reflects the resolution. This is display data only; it
    never substitutes the reviewed snapshot in any security decision."""
    if contract is None:
        contract = TASKS.get_contract(task_id)
    if contract is None:
        return
    if last_decision is None:
        last_decision = _decision_view(task_id)
    STATE.save(task_id, {
        "task_text": _stored_task_text(task_id),
        "contract": contract.model_dump(),
        "last_decision": last_decision,
        "session_reads": sorted(TRACKER.session_asset_ids(task_id)),
        "pending_approvals": APPROVALS.pending(task_id)})


def _finish(task_id: str, contract, request: FlowRequest, lineage: list[str],
            result, decision=None, tainted: bool = False,
            tool_args: dict | None = None,
            declared: list[str] | None = None,
            asset_binding: dict | None = None) -> dict:
    if decision is None:
        decision = ENGINE.evaluate(contract, request)
    _audit(task_id, request, decision, lineage, contract)
    explanation = explain(decision, request, contract, lineage)
    # AD-25: every authorization decision appends a distinct historical row.
    TASKS.append_decision(
        task_id, decision.contract_version, request.tool, request.action,
        request.destination, decision.outcome, decision.reasons,
        _fingerprint(request.tool, {
            "source": request.source, "action": request.action,
            "destination": request.destination,
            "transformation": request.transformation}))
    _save_state(task_id, contract, {
        "decision": decision.model_dump(), "explain": explanation,
        "lineage_path": list(lineage), "tool": request.tool,
        "destination": request.destination})
    agent_message = {"ALLOW": AGENT_MESSAGE_ALLOW,
                     "APPROVE": AGENT_MESSAGE_APPROVE,
                     "BLOCK": AGENT_MESSAGE_BLOCK}[decision.outcome]
    approval_id = None
    if decision.outcome == "APPROVE":
        request_data = request.model_dump()
        request_data["labels"] = sorted(request_data["labels"])
        request_data["source_groups"] = sorted(request_data["source_groups"])
        # FIX1: the record keeps BOTH the policy snapshot (request_data,
        # payload may be truncated) and the exact original tool args, bound
        # to the reviewed contract, with the request's declared sources.
        approval_id = APPROVALS.create(
            task_id, request.tool, request_data,
            tool_args if isinstance(tool_args, dict) else {},
            contract,
            [a for a in (declared or []) if registry.resolve(a) is not None],
            [registry.resolve(a)["data_group"]
             for a in (declared or [])
             if registry.resolve(a) is not None],
            decision.reasons,
            tool_binding=_tool_binding(request.tool),
            asset_binding=asset_binding)
    return {"decision": decision.model_dump(), "agent_message": agent_message,
            "explain": explanation,
            "lineage_path": lineage, "tainted": tainted, "result": result,
            "approval_id": approval_id}


@app.post("/tasks")
def create_task(body: TaskIn):
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    contract = build_task_contract(body.task_text)
    TASKS.create(task_id, body.task_text, contract)
    _mark_live(task_id)
    _save_state(task_id, contract)
    return {"task_id": task_id, "contract": contract.model_dump()}


@app.post("/tasks/{task_id}/tool-call")
def tool_call(task_id: str, body: ToolCallIn):
    contract = TASKS.get_contract(task_id)
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


def _bearer_token(request: Request) -> str:
    """Extract the presented bearer token. Never reads identity from a body
    or any other header: the JWT is the only identity input."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _authenticate(request: Request) -> str | None:
    """Authenticate a human approval endpoint and return the forensic subject.

    JWT mode is fixed by the validated startup configuration, so there is no
    runtime switching and no legacy fallback while it is enabled:

    - JWT disabled: exactly the previous X-Admin-Token behaviour (403 when
      the token is missing/wrong). Returns ``None``; no subject exists.
    - JWT enabled: a valid JWT is required FIRST. A missing/invalid token is
      401 even when X-Admin-Token is correct. Only then is the existing admin
      authorization check applied, so a valid JWT with a missing/invalid
      admin token is 403 (authenticated but not authorized).
    """
    if not CONFIG.jwt_enabled:
        _require_admin(request)
        return None
    if JWT is None:
        # Configuration/verification infrastructure is unavailable. Fail
        # closed; never fall back to legacy authentication.
        raise HTTPException(status_code=503,
                            detail="jwt authentication unavailable")
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401,
                            detail="valid JWT required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = JWT.verify(token)
    except JWTUnavailable:
        raise HTTPException(status_code=503,
                            detail="jwt authentication unavailable") from None
    except JWTRejected:
        raise HTTPException(status_code=401, detail="invalid JWT",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    # Authorization stays separate from identity (AD-06). The validated
    # subject is forensic only and never substitutes for this check.
    _require_admin(request)
    return payload["sub"]


class ResolveIn(BaseModel):
    choice: Literal["allow_once", "expand_task", "deny"]


def _fail_closed(detail: str) -> HTTPException:
    """FIX1: resolution binding failures never execute and never consume
    the approval (status stays pending so the human can re-decide)."""
    return HTTPException(status_code=409, detail=detail)


# Identity fields recorded for forensic attribution (Phase 3, G-04). They are
# returned by GET /audit only to a caller that passes the same authentication
# the approval endpoints require; every other caller gets the trail without
# them (see `read_audit`). Reviewer identity is never part of `reasons`.
IDENTITY_FIELDS = ("subject", "approval_actor")


def _without_identity(events: list[dict]) -> list[dict]:
    """Return the audit trail with reviewer-identity fields removed."""
    for event in events:
        for field in IDENTITY_FIELDS:
            event.pop(field, None)
    return events


def _reviewed_binding(record: dict) -> tuple[TaskContract, FlowRequest]:
    """FIX1: verify the approval's binding to the reviewed contract version.

    The approval record carries the frozen snapshot of the contract the
    human reviewed; the task's current contract is irrelevant to replay.
    Fails CLOSED when the binding is missing or internally inconsistent —
    never falls back to the current contract (kit FIX1-A).
    """
    snapshot = record.get("contract_snapshot")
    stored_id = record.get("contract_id") or ""
    stored_version = record.get("contract_version") or ""
    if not isinstance(snapshot, dict) or not stored_id or not stored_version:
        raise _fail_closed(
            "approval has no verifiable contract binding; fail closed")
    try:
        reviewed = TaskContract.model_validate(snapshot)
    except ValidationError:
        raise _fail_closed(
            "stored contract snapshot is invalid; fail closed")
    if (reviewed.contract_id != stored_id
            or reviewed.version != stored_version):
        raise _fail_closed(
            "approval contract binding mismatch; fail closed")
    try:
        stored = FlowRequest.model_validate(record["request"])
    except ValidationError:
        raise _fail_closed(
            "stored request snapshot is invalid; fail closed")
    return reviewed, stored


@app.post("/approvals/{approval_id}/resolve")
def resolve_approval(approval_id: str, body: ResolveIn,
                     request: Request):
    """Human-only resolution (FIX1), restart-safe for persisted approvals.

    The approval record itself carries the complete frozen reviewed
    binding — contract snapshot + id/version, the EXACT original tool
    args, the stored FlowRequest snapshot and the declared sources — so
    `deny` and `allow_once` NEVER require the task's current contract.
    Task state is durable, but a persisted approval must stay resolvable
    using exactly its reviewed binding (no fallback to any current
    contract, no reconstruction from task text).

    All fail-closed checks run BEFORE the approval is claimed or anything
    executes: missing/invalid snapshot, id/version mismatch, missing exact
    tool args, unknown tool, unresolvable source asset -> 409, status stays
    pending, nothing runs. Three further gates were added after the
    approval-lifecycle review, all pre-claim and all leaving the record
    pending for re-review:

    - R1b: the reviewed tool identity/action/destination AND the connector
      configuration (real-delivery fingerprint) must still match.
    - R3: an approved read is bound to the asset's content digest and
      classification captured at review; changed bytes or labels fail
      closed. The verified bytes are what executes (no second read).
    - R1: the frozen request is re-evaluated against CURRENT policy. A hard
      BLOCK can never be overridden by an earlier human approval; only the
      soft APPROVE case is what the human actually granted.

    expand_task is different by nature: it issues a NEW contract version
    for the task's LIVE review session (I3). A live session is process
    state by nature, so it is NOT persisted: this process must have CREATED
    the task since it started. After a restart (or in a different worker)
    there is no live session to expand and the record is left pending for
    deny / allow_once — a pending expansion approval can never authorize a
    scope change indefinitely. No expiry exists yet (Phase 10); this
    fail-closed rule is the interim guarantee.
    """
    reviewer_sub = _authenticate(request)
    record = APPROVALS.get(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown approval")
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="already resolved")
    task_id = record["task_id"]
    # Deny and allow_once need only the frozen reviewed binding, so they
    # work for any pending record. Expansion additionally needs a live
    # review session in THIS process plus the durable task row.
    task_exists = TASKS.exists(task_id)

    if body.choice == "deny":
        # Deny executes nothing; it stays available even for records with a
        # broken binding so the human can always reject them.
        if not APPROVALS.claim(approval_id, "denied"):
            raise HTTPException(status_code=409, detail="already resolved")
        _audit_event(task_id, {
            "task_id": task_id, "task_text": _stored_task_text(task_id),
            "contract_id": record.get("contract_id") or "",
            "contract_version": record.get("contract_version") or "",
            "tool": record["tool"], "action": "approval_resolve",
            "sources": [], "labels": [], "transformation": "",
            "destination": "", "decision": "BLOCK",
            "reasons": ["Human denied the approval request."],
            "lineage_path": "", "subject": reviewer_sub or ""})
        return {"status": "denied", "executed": False}

    if body.choice == "allow_once":
        # FIX1: single-use, exact replay. Verify the reviewed-contract
        # binding and replayability BEFORE claiming the approval (status
        # stays pending on any failure) or executing the tool. Replay runs
        # under the reviewed contract version, never the current one.
        reviewed, stored = _reviewed_binding(record)
        tool_args = record.get("tool_args")
        if not isinstance(tool_args, dict) or not tool_args:
            raise _fail_closed(
                "approval record lacks the exact original tool args; "
                "fail closed")
        meta = tools.TOOL_REGISTRY.get(record["tool"])
        if meta is None:
            raise _fail_closed(
                "approval references an unknown tool; fail closed")

        # R1b: the human reviewed a specific tool, action, destination and
        # connector configuration. If any of that changed, fail closed and
        # leave the record pending for re-review. Also validate that the
        # side_effect_classification matches — an unclassified or changed
        # classification fails closed.
        reviewed_tool = record.get("tool_binding")
        current_tool = _tool_binding(record["tool"])
        if not isinstance(reviewed_tool, dict) or current_tool is None:
            raise _fail_closed(
                "approval has no verifiable tool/destination binding; "
                "re-review required")
        if (reviewed_tool.get("tool") != current_tool["tool"]
                or reviewed_tool.get("action") != current_tool["action"]
                or reviewed_tool.get("destination")
                != current_tool["destination"]
                or reviewed_tool.get("connector")
                != current_tool["connector"]
                or reviewed_tool.get("side_effect_classification")
                != current_tool.get("side_effect_classification")):
            raise _fail_closed(
                "tool, destination or connector configuration changed since "
                "review; or side_effect_classification mismatch; "
                "re-review required")

        lineage: list[str] = []
        verified_content: str | None = None
        if meta.get("action") in ("read", "query"):
            asset_id = (stored.source
                        if stored.source in registry.ASSETS else None)
            if asset_id is None:
                raise _fail_closed(
                    "approved source asset is no longer resolvable; "
                    "fail closed")
            # R3: an approved read is bound to the content and labels the
            # human reviewed. Verify BEFORE claiming; a mismatch fails
            # closed and leaves the approval pending.
            reviewed_asset = record.get("asset_binding")
            if (not isinstance(reviewed_asset, dict)
                    or reviewed_asset.get("asset_id") != asset_id):
                raise _fail_closed(
                    "approved read has no verifiable content binding; "
                    "re-review required")
            verified_content = registry.read_asset(asset_id)
            verified_labels = sorted(set(classify(asset_id,
                                                 verified_content)))
            if (reviewed_asset.get("content_sha256")
                    != _sha256(verified_content)
                    or list(reviewed_asset.get("labels") or [])
                    != verified_labels):
                raise _fail_closed(
                    "approved asset content or classification changed since "
                    "review; re-review required")
            lineage = [asset_id]

        # R1: current policy decides the hard rules. A hard BLOCK now can
        # never be overridden by an earlier human approval; only the soft
        # APPROVE case is what the human actually granted.
        # Before evaluating policy, log any rejected replay attempt as a
        # security audit event (does not consume approval, create decision,
        # or dispatch side effects — the _fail_closed below will still halt).
        decision = ENGINE.evaluate(reviewed, stored)
        if decision.outcome == "BLOCK":
            raise _fail_closed(
                "current policy hard-blocks this request; a human approval "
                "cannot override a hard rule — re-review required")
        if decision.outcome == "APPROVE":
            decision = decision.model_copy(update={
                "outcome": "ALLOW",
                "reasons": decision.reasons + [
                    "Human approved this single request."],
                "failed_checks": decision.failed_checks})
        # AD-25: the conditional claim is the only way to win resolution.
        if not APPROVALS.claim(approval_id, "allowed_once"):
            raise HTTPException(status_code=409, detail="already resolved")
        if meta.get("action") in ("read", "query"):
            asset_id = lineage[0]
            # The verified bytes are what executes: no second read.
            content = verified_content if verified_content is not None \
                else registry.read_asset(asset_id)
            result = tools.TOOL_FUNCTIONS[record["tool"]](
                {"task_id": task_id, "source_asset": asset_id,
                 "content": content, "verified_content": content},
                **tool_args)
            # Normal read bookkeeping: session registration is idempotent.
            TRACKER.register_asset(task_id, asset_id)
        else:
            content = str(tool_args.get("text")
                          or tool_args.get("payload") or "")
            result = tools.TOOL_FUNCTIONS[record["tool"]](
                {"task_id": task_id, "source_asset": None,
                 "content": content}, **tool_args)
            # Normal outbound bookkeeping: sink edge + full lineage path.
            declared = list(record.get("declared_source_assets") or [])
            payload_node = str(tool_args.get("payload_node")
                               or (declared[0] if declared
                                   else "outbound_payload"))
            TRACKER.record_sink(task_id, payload_node, stored.destination)
            lineage = TRACKER.path_to(task_id, payload_node)
        TASKS.append_decision(
            task_id, decision.contract_version, stored.tool, stored.action,
            stored.destination, decision.outcome, decision.reasons,
            _fingerprint(stored.tool, {"approval_id": approval_id}))
        # Restart-safe: pass the REVIEWED frozen contract explicitly. When
        # the task is live this is identical to its durable current
        # contract; when it is not, the persisted resolution still updates
        # the stored task state (display only) instead of skipping it.
        _save_state(task_id, reviewed, {
            "decision": decision.model_dump(),
            "explain": explain(decision, stored, reviewed, lineage),
            "lineage_path": list(lineage), "tool": stored.tool,
            "destination": stored.destination})
        _audit_event(task_id, {
            "task_id": task_id,
            "task_text": _stored_task_text(task_id),
            "contract_id": reviewed.contract_id,
            "contract_version": reviewed.version,
            "tool": record["tool"], "action": "approval_resolve",
            "sources": [stored.source], "labels": sorted(stored.labels),
            "transformation": "allow_once",
            "destination": stored.destination, "decision": "ALLOW",
            "reasons": ["Human approved this single request."],
            "lineage_path": " -> ".join(lineage),
            "subject": reviewer_sub or ""})
        return {"status": "allowed_once", "executed": True,
                "decision": decision.model_dump(), "result": result,
                "lineage_path": lineage}

    # expand_task: NEW contract version (I3: the old one is never mutated).
    # FIX1-E: bound to the REVIEWED contract version; the grant set comes
    # ONLY from the request's originally DECLARED source groups — never
    # from session-taint source_groups. No declared group => fail closed.
    # F2: expansion is a live-session operation, so it requires BOTH the
    # durable task row and a session in this process. After a restart the
    # record stays pending (deny / allow_once remain available) rather than
    # letting a stale approval expand scope indefinitely.
    if not (task_exists and _session_is_live(task_id)):
        raise _fail_closed(
            "no live review session for this task in this gateway process; "
            "restart-safe resolution supports deny and allow_once only")
    reviewed, stored = _reviewed_binding(record)
    declared_assets = list(record.get("declared_source_assets") or [])
    declared_groups = sorted({
        registry.resolve(asset)["data_group"]
        for asset in declared_assets if registry.resolve(asset) is not None
    } & catalog.DATA_GROUPS)
    if not declared_groups:
        raise _fail_closed(
            "no declared source group to expand; fail closed")
    new_allowed_data = sorted(set(reviewed.allowed_data)
                              | set(declared_groups))
    new_actions = sorted(set(reviewed.allowed_actions) | {stored.action})
    new_contract = TaskContract(
        contract_id=reviewed.contract_id + "-x",
        parent_contract_id=reviewed.contract_id,
        purpose=reviewed.purpose,
        allowed_data=new_allowed_data,
        allowed_destinations=list(reviewed.allowed_destinations),
        allowed_actions=new_actions,
        external_transfer=reviewed.external_transfer,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not APPROVALS.claim(approval_id, "expanded"):
        raise HTTPException(status_code=409, detail="already resolved")
    TASKS.set_contract(task_id, new_contract)
    TASKS.append_decision(
        task_id, new_contract.version, record["tool"], "approval_resolve",
        "", "ALLOW", ["Task scope expanded by human resolution."],
        _fingerprint(record["tool"], {"approval_id": approval_id}))
    _save_state(task_id, new_contract)
    _audit_event(task_id, {
        "task_id": task_id,
        "task_text": _stored_task_text(task_id),
        "contract_id": new_contract.contract_id,
        "contract_version": new_contract.version,
        "tool": record["tool"], "action": "approval_resolve",
        "sources": [stored.source], "labels": sorted(stored.labels),
        "transformation": "expand_task",
        "destination": stored.destination, "decision": "ALLOW",
        "reasons": [f"Task scope expanded to contract version "
                    f"{new_contract.version[:12]}…; parent "
                    f"{reviewed.contract_id} unchanged."],
        "subject": reviewer_sub or "",
        "lineage_path": ""})
    return {"status": "expanded",
            "contract": new_contract.model_dump()}


@app.get("/approvals")
def list_approvals(task_id: str | None = None):
    """Narrow pending list for orchestration; no args, no payload."""
    return {"pending": APPROVALS.pending(task_id)}


@app.get("/approvals/{approval_id}")
def approval_detail(approval_id: str, request: Request):
    """FIX1-C: the human approval preview. Shows the EXACT request being
    approved — tool, action, declared source group(s), destination, and the
    full outbound payload — plus the reviewed contract binding.

    Full tool_args are sensitive approval state (DECISIONS §10): this
    endpoint is admin-gated like resolution (403 without the token), and
    the generic unauthenticated pending list above stays narrow.
    """
    _authenticate(request)
    record = APPROVALS.get(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown approval")
    request_snap = record.get("request") or {}
    tool_args = record.get("tool_args") or {}
    return {
        "approval_id": record["approval_id"],
        "task_id": record["task_id"],
        "status": record["status"],
        "tool": record["tool"],
        "action": request_snap.get("action", ""),
        "destination": request_snap.get("destination", ""),
        "source_groups": sorted(request_snap.get("source_groups") or []),
        "declared_source_groups": list(
            record.get("declared_source_groups") or []),
        "payload": tool_args.get("text") or tool_args.get("payload")
                   or request_snap.get("payload", ""),
        "tool_args": tool_args,
        "contract_id": record.get("contract_id", ""),
        "contract_version": record.get("contract_version", ""),
        "reasons": record.get("reasons", ""),
        "created_at": record.get("created_at", ""),
    }


@app.get("/tasks/{task_id}")
def task_state(task_id: str):
    contract = TASKS.get_contract(task_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="unknown task")
    projection = STATE.get(task_id) or {}
    return {"task_id": task_id, "task_text": TASKS.task_text(task_id),
            "contract": contract.model_dump(),
            "last_decision": (projection.get("last_decision")
                              or _decision_view(task_id)),
            "session_reads": sorted(TRACKER.session_asset_ids(task_id)),
            "pending_approvals": APPROVALS.pending(task_id)}


@app.get("/audit")
def read_audit(request: Request, task_id: str | None = None):
    """Append-only audit trail.

    The trail itself stays readable (the demo readiness probe and the
    agent-side runner depend on that), but reviewer identity is withheld
    unless the caller passes the SAME authentication the approval endpoints
    require. Unauthenticated callers therefore cannot learn who approved what,
    and no error is raised that would distinguish "redacted" from "absent".
    """
    events = AUDIT.read_all(task_id)
    try:
        _authenticate(request)
    except HTTPException:
        return {"events": _without_identity(events)}
    return {"events": events}


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
