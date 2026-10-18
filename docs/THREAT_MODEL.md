# TaskFence — Threat Model

**Phase:** -1 (design-only artifact)
**Status:** Accepted as the working threat model. This document describes threats and *required* mitigations. It is **not** a claim that any listed mitigation is implemented.
**Evidence snapshot:** checked-out tree at commit `bf6418f` ("Import cloud project files"). Every evidence citation below was read from that tree. Line numbers are valid for that commit only.

**Evidence status vocabulary used throughout:**

| Status | Meaning |
|---|---|
| `PRESENT` | Implemented in this tree and covered by a named test. |
| `PARTIAL` | Some code or a partial test exists; the stated property is not fully covered. |
| `ABSENT` | No implementation in this tree. |
| `VERIFY` | Cannot be established from this tree; the owning phase must confirm before relying on it. |

---

## 1. Verified current state (baseline for Phase 0)

This section is the evidence base for every claim elsewhere in this document. Phase 0 formalizes it; nothing here should be re-asserted later without re-checking.

### 1.1 Security authority

- Single policy engine: `taskfence/policy.py` is deterministic, imports only stdlib `enum`/`typing` plus `taskfence.catalog` and `taskfence.models`, and performs no network, file, or clock access. `PRESENT` — `tests/test_policy.py`, `tests/test_invariants.py`.
- Default deny for unknown tool/asset/destination/action: `policy.py` emits hard failures for unknown vocabulary (`HARD_UNKNOWN_ENTITY`). `PRESENT` — `tests/test_policy.py`.
- Immutable contracts: `TaskContract` is `frozen=True` with a computed content-hash `version` (`taskfence/models.py`). `PRESENT` — `tests/test_models.py`, `tests/test_contract.py`.
- The gateway is the only execution point for tools: `taskfence/tools.py` is private; agent-side code imports only `taskfence/client.py`. `PRESENT` — `tests/test_no_bypass.py` (static import check, explicitly code hygiene, not isolation).
- MCP is an adapter: `mcp_gateway/server.py` contains no decision logic and forwards to `GatewayClient`; outcomes are returned verbatim. `PRESENT` — `tests/test_mcp_adapter.py`, `tests/test_mcp_scenarios.py`.

### 1.2 Persistence surfaces (all SQLite today)

| Surface | Location | Writer | Reader |
|---|---|---|---|
| `audit_events` (append-only) | `taskfence/audit.py` | `AuditLog.log_event` | `AuditLog.read_all`, gateway `/audit`, dashboard |
| `task_state` (display snapshot) | `taskfence/audit.py` (`TaskStateStore`) | gateway `_save_state` | dashboard only |
| `approvals` | `taskfence/approvals.py` | `ApprovalStore.create`, `set_status` | gateway, dashboard |
| `lineage_nodes` / `lineage_edges` | `taskfence/lineage.py` | `LineageTracker._add_node/_add_edge` | gateway, dashboard |

Two process-local structures are **authoritative for security decisions** and are not covered by any of the tables above:

- `CONTRACTS`, `TASK_TEXT`, `LAST_DECISION` — module-level dicts at `taskfence/gateway.py:55-57`. `tool_call` reads `CONTRACTS.get(task_id)` (`gateway.py:289`) and returns 404 for an unknown task, so a contract created in one process is invisible to another process. `TaskStateStore` at `gateway.py:53` persists a **display-only** snapshot, not the decision input.
- `LineageTracker.session_reads` — in-memory dict at `taskfence/lineage.py:41`, populated by `register_asset` (`:72`) and consumed by `effective_labels` (`:141`). This is a security input: conservative-mode taint reaches `_handle_outbound` label computation. It is not persisted or reloaded.

### 1.3 Identity and approval

- No end-user authentication. The only credential check is `_require_admin` (`gateway.py:302-309`), an `X-Admin-Token` comparison against `TASKFENCE_ADMIN_TOKEN`; it gates approval detail (`:548`) and resolution (`:377`).
- Approval records are SQLite-backed and carry the reviewed contract snapshot plus exact `tool_args` (FIX1). `PRESENT` — `tests/test_approval_integrity.py`, `tests/test_approval_persistence.py`.
- `ApprovalStore.set_status` (`approvals.py:149-155`) is `UPDATE … WHERE approval_id = ?` with **no current-state guard**. The gateway checks `record["status"] != "pending"` (`gateway.py:~381`) and later calls `set_status`, so the check and the transition are not atomic.
- No threaded/concurrent approval test exists. `grep` for `Thread`/`ThreadPool` in `tests/` matches only unrelated files.

### 1.4 Models and credentials

- Contract construction: local Ollama proposal + deterministic validator + keyword fallback (`taskfence/contract.py`). The LLM proposes, code validates. `PRESENT` — `tests/test_contract.py`.
- Agent runtimes: `ScriptedAgent`, `OllamaAgent` (`max_steps = 8`), `McpAgent` (`taskfence/agent.py`). All tool calls go through `GatewayClient`.
- No cloud model provider.
- No credential broker. `taskfence/tools.py` reads `TASKFENCE_REAL_SLACK_WEBHOOK` directly from the process environment and performs at most one HTTP POST on the ALLOW path. The URL is not logged or audited. `PRESENT` (behaviour) / `ABSENT` (broker).
- `taskfence/registry.py` holds asset metadata only (paths under `data/`, sources, groups, labels). It contains no credentials.

### 1.5 Configuration and test surface

- Environment reads are scattered across `contract.py`, `agent.py`, `client.py`, `gateway.py`, `tools.py`, `mcp_gateway/server.py`, `dashboard/app.py`; there is no config module and no `.env.example`.
- `TASKFENCE_DB` is honored by `dashboard/app.py:23,545` and by tests, but **not** by `audit.py` / `approvals.py` / `lineage.py`, which hardcode `Path("taskfence.sqlite3")`. A dashboard pointed at a different database than the gateway writes will silently disagree about security state.
- `pytest.ini` defines `ollama` and `live_integration` markers with `addopts = -m "not ollama and not live_integration"`. No `postgres_integration` / `redis_integration` tiers exist.
- No CI workflow exists.
- `tests/test_copy_claims.py` bans overclaiming phrases across `README.md`, `docs/`, `dashboard/`, `taskfence/` and requires specific honesty lines in the README.

### 1.6 Known, disclosed limitations carried forward

- Regex classification is coarse (`taskfence/classifier.py`).
- Conservative lineage can over-block (documented in `taskfence/lineage.py` module docstring).
- The dashboard is not hardened or isolated (`dashboard/app.py`, 1088 lines, reads the SQLite file directly).
- Gateway SQLite connections are created per store with `check_same_thread=False` and no connection pool; concurrent FastAPI workers share one file, not one transactional boundary.

---

## 2. Assets

| ID | Asset | Why it matters | Current protection |
|---|---|---|---|
| A1 | Provider credentials / webhook URLs (`TASKFENCE_REAL_SLACK_WEBHOOK`, future OAuth access/refresh tokens) | Direct compromise of downstream systems | Env var only; read inside the tool implementation; never logged or audited |
| A2 | User identity and principal claims | Everything downstream is authorized against it | `ABSENT` — no user identity exists |
| A3 | Task contracts (`TaskContract` + version) | Define intent; must never become a capability grant | Frozen model; content-hash version |
| A4 | Source enterprise data under `data/` | The thing being protected | Fake fixtures only today |
| A5 | Derived data and lineage graphs | Inherited restrictions must follow transformations | In-process graph + SQLite mirror; session taint in memory only |
| A6 | Pending approvals + reviewed bindings | Human decisions that authorize a single action | SQLite; FIX1 snapshot binding; admin-token gated |
| A7 | Audit history | Sole forensic record of decisions | Append-only SQLite table; stores `task_text` and truncated payload |
| A8 | Destination / resource registry | Determines whether data movement is allowed | Controlled vocabulary in `taskfence/catalog.py` |
| A9 | Budget / rate accounting | Stops a runaway authorized agent | `ABSENT` |
| A10 | Kill-switch state | Emergency stop that must not be bypassed | `ABSENT` |
| A11 | Authentication key material (JWKS, admin token) | Forgery / impersonation | Admin token only; JWKS `ABSENT` |

---

## 3. Trust boundaries

```text
[ untrusted: user task text ] --(I7: intent only, never content)--> TaskFence
                                                                       |
[ untrusted: model provider ] --tool proposal only-------------------> |
[ untrusted: agent runtime  ] --GatewayClient only-------------------> |
[ semi-trusted: MCP adapter ] --adapter, no decisions-----------------> |
                          TaskFence Gateway  <-- ONLY security authority
                             |          |           |
                    [ credential   [ SQLite       [ tool impls
                      broker ]       state ]        (fake sinks,
                             |          |            real Slack POST)
                             v          v
                        [ provider ] [ audit / lineage / approvals ]
```

Boundary statements that must remain true:

- **B1** Model → gateway: a proposal, never an authorization. No adapter may classify, evaluate policy, compute lineage, or execute.
- **B2** Agent → gateway: `GatewayClient` only. A Python import boundary is logical separation, not process isolation.
- **B3** Gateway → provider: credentials resolved only after ALLOW, only inside the executing tool.
- **B4** Gateway → control-plane state: authoritative reads/writes must be durable, not process-local.
- **B5** Operator → approvals/reconciliation: authenticated, explicitly authorized, audited.
- **B6** Dashboard → state: read-only display; it computes no security decision.

---

## 4. Threat table

`ID` values are referenced by `docs/GAP_REGISTER.md`.

| ID | Threat | Asset | Preconditions | Required mitigation | Evidence today | Residual risk | Owner |
|---|---|---|---|---|---|---|---|
| T-01 | Contract privilege escalation — task text widens what the user may move | A3 | Contract treated as a grant | Principal authorization AND delegation AND task policy; contract grants nothing | `ABSENT`; only task policy exists | Principal model is the product's core claim | Phase 4 |
| T-02 | Spoofed identity / body-supplied identity | A2 | No token validation | Verified JWT (iss, aud, alg, exp, JWKS); body identity ignored; 401 in JWT mode | `ABSENT` | Total today: no identity at all | Phase 3 |
| T-03 | Self-approval of a high-risk action | A6 | Approver identity equals requester | `taskfence:approval:resolve` from verified claims; deny self-approval for high risk; deny when comparison is unreliable | `ABSENT` (single shared admin token) | Any token holder can approve anything | Phase 3/4 |
| T-04 | Approval replay / double execution | A6 | Two resolutions race, or a resolved record is re-resolved | Atomic claim with a state guard before dispatch; single-use | `PARTIAL` — sequential double resolution is tested (`test_approval_persistence.py:214`); `set_status` has no state guard; no concurrent test | **Real race today** (G-03) | Phase 10 |
| T-05 | Malicious/compromised in-process agent reads secrets or process memory | A1, A4 | Agent code executes in the gateway process | Real process/OS boundary, or honest disclosure that none exists | `ABSENT`; disclosed as code hygiene only | Accepted, disclosed limitation | Deferred (explicit non-goal) |
| T-06 | Credential exposure through prompts, audit, approvals, MCP, exceptions, subprocess env | A1 | Secret reaches a readable sink | Broker + redaction; subprocess env allowlist; refuse OAuth connectors without secure token storage | `PARTIAL` — webhook read only inside `tools.send_slack`; never logged/audited (`tools.py`); MCP subprocess env untested | Untested leak paths | Phase 6 |
| T-07 | Untrusted retrieved content steers the agent (injection) | A3, A4 | Agent consumes attacker-influenced text | Containment: policy adjudicates the *movement*, not the model's intent | `PARTIAL` — Scenario B demonstrates containment (`tests/test_scenarios.py`) | Injection itself is not prevented; by design | Ongoing, non-goal to eliminate |
| T-08 | Task text used to derive contract scope from tool output | A3 | Contract builder consumes retrieved content | I7: contract built from task text only | `PRESENT` — `build_contract` signature and tests | — | Maintenance |
| T-09 | Unauthorized destination / lookalike host | A8 | Coarse destination vocabulary, substring matching | Exact registered resource identity; reject substring lookalikes; unknown → BLOCK | `PARTIAL` — closed vocabulary blocks unknown names; no per-channel/per-repo granularity, no URL parsing | Coarse but default-deny | Phase 5 |
| T-10 | Cross-process disagreement about task state | A3, A5 | Two gateway workers, same DB | Durable authoritative task state + cross-process test | `ABSENT` — dicts at `gateway.py:55-57`; `task_state` is display-only | **Real today** (G-01) | Phase 2 |
| T-11 | Lost conservative taint after restart | A5 | Session read sensitive data, then process restarts | Persist session read set with the task | `ABSENT` — `lineage.py:41` in memory only | **Real today** (G-02) | Phase 2 |
| T-12 | Stale security state used to authorize | A3, A6, A10 | DB/Redis unavailable | Fail closed: deny protected action; never authorize from last-known ALLOW | `ABSENT` (decision recorded in AD-05) | Availability trade-off accepted | Phase 10 |
| T-13 | Kill switch bypassed across processes | A10 | Kill applied in one worker only | Durable shared state; local override may only add denial | `ABSENT` | — | Phase 10 |
| T-14 | Budget bypass / double-spend under retry or concurrency | A9 | Concurrent or repeated requests | Atomic check+dedupe+increment+expiry (single Lua `/eval`); reservation before dispatch | `ABSENT` | — | Phase 7 |
| T-15 | Runaway authorized agent (volume, cost, time) | A9 | Allowed operations loop | Turn/token/time/volume budgets owned by the gateway | `ABSENT`; `MAX_STEPS = 8` is agent-side only and not enforced by the gateway | Agent could be replaced | Phase 7 |
| T-16 | Ambiguous external side effect retried into a duplicate send | A1, A7 | Timeout after dispatch; non-idempotent provider | Durable claim before dispatch; unknown-outcome state; no blind retry; reconciliation | `ABSENT` — `tools.send_slack` POSTs with no idempotency key or claim record | — | Phase 10/11 |
| T-17 | Audit tampering / audit as a second data leak | A7 | Write access to SQLite; payload stored verbatim | Append-only API; no secrets; minimized payloads; retention policy | `PARTIAL` — no update/delete API (`audit.py`); stores `task_text` and `payload[:200]`; no retention policy | Real minimization gap | Phase 10 |
| T-18 | Gateway bypass via MCP or a second policy path | A3, A8 | Adapter grows local logic | Adapter-only rule; one authorization composition for MCP and non-MCP | `PRESENT` — `tests/test_mcp_adapter.py`, `tests/test_no_bypass.py` | — | Maintenance |
| T-19 | Forged/`alg:none` token or JWKS fetch storm | A11 | Attacker-influenced tokens | RS256-only, bounded single-flight refresh, reject on unresolved `kid`/outage | `ABSENT` | — | Phase 3 |
| T-20 | Provider data-use mismatch (free tier ingesting non-synthetic data) | A4 | Real data routed to a free-tier model | Explicit ENV/DATA_MODE discriminator; two operator assertions; fail closed | `ABSENT` | — | Phase 8 |
| T-21 | Quota exhaustion / free-tier pause taking the product offline | All | Free tiers have quotas, pauses, no SLA | Fail closed, never downgrade; local-first demo; no silent fallback | `ABSENT` | Availability risk accepted and disclosed | Phase 12/13 |
| T-22 | Dashboard diverges from gateway state | A7 | Dashboard reads a different SQLite path than the gateway writes | Single configuration source for the DB path | `ABSENT` — `TASKFENCE_DB` honored by dashboard only | Misleading operator view | Phase 1/2 |

---

## 5. Explicit non-goals

These are not promises weakened later; they are out of scope for this cycle and must be stated as such in product copy.

1. No claim of complete prompt-injection prevention. Containment of *data movement* is the product; model behaviour is not controlled.
2. No complete DLP. Classification is regex-based and coarse.
3. No sandbox. Same-process Python separation is logical only until a real OS/container boundary is built and tested.
4. No compliance certification (SOC 2, ISO 27001, HIPAA, etc.).
5. No guarantee against a compromised host, container, or process.
6. No universal exactly-once delivery to third-party APIs.
7. No enterprise IAM, multi-agent federation, SIEM integration, Kubernetes, multi-region HA, or exhaustive connector coverage.

---

## 6. Credential boundary statement

**What is isolated today:** the webhook value is read inside `tools.send_slack` and is never returned in the tool result, never written to audit, and never included in `GatewayClient` responses. `registry.py` contains no secrets.

**What is not isolated:** agent-side and gateway code share one Python process and one environment. Any code executing in that process can read `os.environ`. The import-graph check in `tests/test_no_bypass.py` prevents accidental imports; it does not prevent deliberate access.

**Therefore:** documentation must never describe the credential broker as a sandbox. Hard protection requires a separate process/OS/container boundary with its own tested threat model.

---

## 7. Approval model

- **Permission:** `taskfence:approval:resolve`, sourced only from verified token claims or a trusted server-side mapping. Never from request JSON, tool arguments, or model output.
- **Requester/approver comparison:** for high-risk actions the requester cannot approve their own request. When the two identities cannot be compared reliably, resolution is denied.
- **Atomicity:** the claim and the audit write must commit together before dispatch. A denial stays a denial even if audit storage is unavailable; the audit failure is reported separately and never converted into ALLOW.
- **High-risk definition:** configured per action/destination/classification. If unconfigured, the conservative default applies: require an approver distinct from the requester.
- **Audit:** requester, approver, authorization basis, prior/new state, decision, reason, approval ID, timestamp.
- **Not yet implemented:** the permission predicate, the self-approval rule, and the atomic claim. Only the shared admin token exists today.

---

## 8. Latency baseline

`VERIFY`. No measurement exists in this tree. Phase 9 must record p50/p95 for a local deterministic ALLOW and a local deterministic BLOCK path before and after instrumentation, using the same corpus and the same process configuration, and the human reviewer must set a project-specific target from those numbers. No universal target is asserted here.

---

## 9. Open decisions

`AD-05` (availability posture), `AD-13` (local/deployed discriminator), and `AD-16` (reconciliation transitions) are **decided** in `docs/ARCHITECTURE_DECISIONS.md`. No blocking design decision remains open; every implementation gap is tracked in `docs/GAP_REGISTER.md` with an owner phase.
