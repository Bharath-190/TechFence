# TaskFence — Architecture Decisions

**Phase:** -1 (design-only artifact)
**Status:** Accepted design decisions for the free-stack cycle. These record *what later phases must implement and be tested against*. They are not claims about the current tree; current-state evidence lives in `docs/THREAT_MODEL.md` §1 and `docs/GAP_REGISTER.md`.

**Approvals:** `AD-05` was approved by the human reviewer on 2026-10-17 (record in AD-05). It is the only decision in this file that required explicit human sign-off before Phase 1.
**Evidence snapshot:** commit `bf6418f`.

Format: Context → Decision → Consequences → Verification. Status is `Accepted` unless noted.

---

## AD-01 — Security authority is composition, not a task contract

**Context.** Today `taskfence/policy.py` is the only gate. A `TaskContract` therefore acts as a capability: whoever writes the task text widens what may move. That is the product's central risk, not an edge case.

**Decision.** Effective access is the conjunction of three independent checks, evaluated in this order and recorded separately:

```
user/organization authorization
  AND agent/delegation authorization
  AND TaskFence task policy
```

A `TaskContract` expresses intent. It grants nothing. Composition wraps the existing engine; the engine is not rewritten.

**Consequences.** A principal model and a delegation model must exist before any real connector is enabled. Local demo mode needs an explicit, documented principal rather than an implicit superuser — otherwise scenarios A–G exercise policy only and the product claim is unproven in its own demo.

**Verification.** Negative matrix: task allows a group the user lacks → DENY; user has a group the task excludes → task-policy semantics; agent outside delegation → DENY; unknown principal/resource/action → DENY; changing task text never elevates. MCP and non-MCP paths must produce identical outcomes.

---

## AD-02 — Durable state is authoritative; SQLite mirrors are projections

**Context.** `CONTRACTS`/`TASK_TEXT`/`LAST_DECISION` are module dicts (`gateway.py:55-57`), `session_reads` is in-memory (`lineage.py:41`), and `task_state` is a persisted snapshot that is documented as display-only. A second worker, or a restart, therefore changes security behaviour (see G-01, G-02).

**Decision.** After Phase 2, durable persistence is the sole authority for tasks, contracts, approvals, audit, lineage, last decision, and session taint. `task_state` remains a **projection for the dashboard**. No module-level dict may be the source of truth for security or task state. Where a cache exists, it must be reconstructible and must never authorize a new state-changing operation.

**Consequences.** Every decision path acquires a read from durable storage; this is why AD-05 matters. Projections may lag; decisions may not.

**Verification.** Cross-process test: create a contract through process A, resolve a tool call through process B against the same `TASKFENCE_DATABASE_URL`. Restart test: session taint survives. Projection-only fields must never be read by a decision path.

---

## AD-03 — Reserve the schema once; migrate forward only

**Context.** Phases 3, 5, 9 and 10 each add fields the audit/approval/task tables will need. Literal per-phase migration would rewrite the same tables three times.

**Decision.** Phase 2 designs the full column set up front, using nullable columns or related tables until each phase populates them. Migrations are versioned and forward-only, safe to rerun, and must not discard audit/approval/lineage rows. Every reserved field is documented with the phase that begins writing it.

Reserved at minimum: tenant/org, authenticated subject, agent id, session id, policy version, trace/correlation id, idempotency key / request hash, approval actor + authorization basis, resource/destination ids, kill-switch state, reconciliation state.

**Consequences.** Some columns are null for several phases; queries and tests must tolerate that. No destructive rewrites later.

**Verification.** Migration from current schema; second run is a no-op; pre-existing audit/approval/lineage rows survive; each reserved column has a documented owner phase.

---

## AD-04 — SQLite is local compatibility; PostgreSQL is the concurrency authority

**Context.** All persistence today is SQLite with per-store connections. SQLite cannot demonstrate row-level locking, concurrent claims, or constraint behaviour under contention.

**Decision.** SQLite remains the default for the free/local demo. PostgreSQL is authoritative for multi-process and concurrency acceptance. SQL is explicit (no ORM). Dialect differences are identified in Phase -1 and both dialects are tested where the behaviour differs; concurrency and atomicity claims are accepted **only** from PostgreSQL runs.

**Consequences.** Anything relying on `SELECT … FOR UPDATE`, unique-constraint arbitration, or transactional DDL must be implemented and tested on PostgreSQL first; SQLite may degrade to a serialized path. A single SQLite file shared by two workers is not a supported production configuration.

**Verification.** PostgreSQL integration tier for transaction boundaries, unique constraints, concurrent claims; SQLite tier for the local demo path. SQLite passing is never accepted as evidence of PostgreSQL concurrency correctness.

---

## AD-05 — Availability posture: fail closed, and say so (blocking decision)

**Context.** A security gateway is a dependency in the request path. The free-stack profile has no SLA. "Stay available" and "never authorize from stale state" are in direct tension.

**Decision.** If any of the following cannot be read or durably written, the protected action is **denied**:

- authoritative task/contract state
- principal or delegation authorization
- kill-switch state
- a budget dimension the operation is required to satisfy
- the audit/claim write that must precede dispatch

Specifically: no cached decision may authorize a new state-changing operation; no last-known ALLOW may be reused; no process-local substitute may stand in for shared state outside the fully local configuration.

**Availability consequence (accepted and must be documented).** A control-plane outage (PostgreSQL, Redis where required, JWKS in JWT mode) stops protected actions. The product is unavailable rather than permissive. Only optional components — telemetry export, dashboard rendering, non-authoritative projections — may fail without affecting authorization.

**Consequences.** Readiness must reflect the dependencies each operation actually needs. Timeouts must be bounded so "cannot be read" resolves to a denial rather than a hang. The demo/walkthrough is local-first for exactly this reason.

**Verification.** For each protected operation: with the authoritative dependency unavailable, the outcome is DENY/BLOCK and an audit/report of the failure; a second process agrees; telemetry failure alone changes nothing.

### Approval record (human reviewer, 2026-10-17)

**Decision:** approved as written for the current TaskFence prototype. This closes the Phase -1 decision gate for AD-05.

The approved policy, binding on Phases 2–13:

1. The gateway fails closed for **every protected action** whenever authoritative task/contract state, required authorization state, kill-switch state, required risk-budget state, or required audit/claim persistence cannot be verified.
2. No fallback to stale authorization decisions, local in-memory authoritative state, or permissive defaults.
3. This is a **protected-action availability policy**, not a requirement to crash the entire application. Optional telemetry and dashboard rendering may fail independently, provided the security decision and the required audit guarantees remain intact.

**Consequences of acceptance (normative):**

- Denial is scoped to the protected action. Health/liveness, static assets, and dashboard read paths may continue to serve while protected actions are denied.
- "Cannot be verified" includes connection failure, timeout, malformed/inconsistent state, and state that cannot be proven current — not only "service unreachable".
- Readiness must distinguish *process alive* from *able to authorize safely*; the two are different signals and must not be collapsed into one boolean that reports healthy while protected actions are denied.
- Phase 10 must test both halves: the protected action is denied **and** the process still serves the non-security endpoints.
- No later phase may add an availability-motivated fallback (cached decision, last-known ALLOW, in-memory substitute for shared state) without a new recorded human decision. "The demo needs to keep working" is not such a decision.

---

## AD-06 — Identity is forensic and authorization is separate

**Context.** Today one shared admin token gates approval detail and resolution (`gateway.py:302-309`).

**Decision.** JWT mode validates issuer, audience, signature restricted to the configured algorithm (RS256), expiry, and standard claims, with a bounded JWKS cache. Identity is attached to task state and audit, and never grants access by itself. Approval resolution requires `taskfence:approval:resolve` from verified claims or a trusted server-side mapping. Body-supplied identity is ignored. Invalid/missing token in JWT mode is 401.

**Consequences.** Local mode keeps an explicit local principal (see AD-01); JWT mode has no anonymous fallback. Approval authority is a tested predicate, never the phrase "admin-gated".

**Verification.** Token matrix incl. `alg:none`, wrong key/algorithm, wrong issuer/audience, expired, missing; body identity cannot override token identity; audit records the authenticated subject and authorization basis; approval resolution rejects missing/ambiguous permission.

---

## AD-07 — Delegation is strictly attenuating

**Context.** "Delegated permissions" is meaningless if a delegate can hold more than the delegator.

**Decision.** Effective delegated permissions are the intersection of the delegator's permissions and the requested delegation. A delegate can never gain actions, resources, destinations, classifications, or budgets the delegator lacks. Delegation is data, not inference: it is resolved by the gateway, never supplied by the model or the agent.

**Verification.** Delegate requesting a superset → DENY; narrower delegation → allowed; unknown delegation → DENY; task expansion cannot manufacture delegation.

---

## AD-08 — Resource and destination identity is exact, and the registry holds no secrets

**Context.** `catalog.DESTINATIONS` is a small closed vocabulary (`sales_slack`, `external_api`, `hr_portal`, `internal_wiki`, `repo`). Unknown names already BLOCK, but there is no per-channel, per-repo, per-folder, or host-level identity.

**Decision.** Authorization resolves to concrete registered resources (Slack workspace/channel, Drive file/folder, GitHub repository, CRM dataset scope, approved vendor endpoint). Matching is exact identity, never substring. `external_transfer=true` never implies "any internet destination". Lookalike/subdomain tricks must be rejected explicitly. The registry carries no credentials.

**Verification.** #sales vs #finance; approved vendor vs arbitrary host; subdomain/lookalike cases; resource mismatch; unknown resource → BLOCK; unregistered contract destination → BLOCK.

---

## AD-09 — Credential boundary is logical separation, disclosed as such

**Context.** `tools.py` reads `TASKFENCE_REAL_SLACK_WEBHOOK` from the process environment. Agent and gateway share a process.

**Decision.** A `CredentialProvider` interface is introduced, gateway-only, resolving credentials only after ALLOW and only into the executing tool. BLOCK and APPROVE never resolve credentials. The interface is designed now for per-principal/per-tenant, scoped, expiring, refreshable, revocable credentials, even while the first implementation is environment-backed. Refresh tokens are never exposed to model, agent, or MCP payloads, and no live OAuth connector is enabled until secure token storage and refresh/revocation are implemented and tested. MCP subprocesses receive an explicit env allowlist, never the full parent environment.

**Consequences.** Documentation must state that in-process separation does not stop a compromised in-process agent. Import-graph checks are code hygiene. A real boundary is a separate process/OS/container and is out of scope until a threat model and boundary tests exist for it.

**Verification.** Principal/resource/scope mismatch rejected; expired/revoked/unrefreshable → fail closed; BLOCK/APPROVE resolve zero credentials; ALLOW resolves exactly once; exception strings and audit contain no secret; subprocess env allowlist enforced.

---

## AD-10 — The gateway owns budgets: reserve before dispatch, settle after execution

**Context.** Call-count budgets are knowable before execution; record/volume budgets are only knowable from the tool's actual behaviour. Connectors arrive in Phase 11, but unused budget controls must not ship as decoration.

**Decision.**

- Gateway reserves tool-call and outbound-call counts atomically **before** dispatch.
- Tools/connectors return a typed usage envelope (records/items, bytes). The gateway alone settles those dimensions after execution. Model-supplied counts are never trusted.
- A tool that cannot report a required dimension must not be advertised as enforcing it: either the budgeted operation fails closed, or the dimension is marked unsupported and policy cannot claim enforcement.
- Dimensions: tool calls, outbound calls, records read, outbound messages, model turns / agent-loop iterations, tokens or provider-reported usage where trustworthy, execution time, optional data volume.
- Turn and token limits are gateway/runtime-owned; when provider usage is unreliable, a conservative turn/request-size cap applies instead.
- Reservation/refund rules are documented for BLOCK, APPROVE, failure-before-dispatch, and unknown external outcome. Retries under the same idempotency key never double-spend.

**Consequences.** Phase 7 intentionally modifies the shared tool-result contract and the local fake sinks, backwards-compatibly where feasible. Phase 11 connectors must emit the same envelope; no connector-specific budget semantics.

**Verification.** Below/at/over limit; repeated request cannot bypass; concurrent requests cannot double-spend; duplicate idempotency key deduplicated atomically; unavailable accounting → fail closed; a runaway agent loop is stopped by the turn cap; no false enforcement claim without trustworthy usage.

---

## AD-11 — Upstash counters use one atomic operation

**Context.** `INCR` followed by `EXPIRE` over REST is two round trips; a crash between them leaves a counter with no expiry, and concurrent callers can both observe "under limit".

**Decision.** Use a single Redis Lua script through the Upstash REST `/eval` endpoint that atomically checks limits, deduplicates the idempotency key, increments counters, and applies expiry. If that operation cannot be verified against the current Upstash REST API, mark it `VERIFY` and do not claim shared/concurrency-safe budgets until a verified atomic primitive exists. Local in-process accounting is permitted **only** for the fully local configuration and is never a fallback for a deployed or multi-process configuration.

**Verification.** Atomicity and dedupe tested against a real Redis/Upstash-compatible service, not a mock; Redis unavailable → fail closed where shared accounting is required.

---

## AD-12 — Model providers are adapters with no security authority

**Context.** Two model paths are in scope: Ollama/Qwen3 (local regression and demo) and Gemini (first cloud provider).

**Decision.** A provider abstraction normalizes tool calls to `{tool, arguments}`. Adapters may format prompts, declare tool schemas, and parse responses. Adapters may not evaluate policy, classify data, compute lineage, decide ALLOW/APPROVE/BLOCK, or execute tools. Every returned function call enters the same `GatewayClient` path as a scripted agent. Unknown tool → gateway default deny; malformed arguments → fail closed. Tool results are fed back to the model only after the gateway has authorized and executed.

**Consequences.** Provider swap changes nothing about authorization. The MCP path stays independent and adapter-only.

**Verification.** Normalization, function-call parsing, unknown tool, malformed args, BLOCK prevents execution, APPROVE creates an approval without executing, ALLOW uses the same gateway path as Qwen3, model cannot override a gateway BLOCK, timeout/error never triggers direct execution.

---

## AD-13 — Local vs deployed is a deterministic discriminator, not a guess (blocking decision)

**Context.** `TASKFENCE_DATA_MODE` alone cannot be both a safe local default and a hard deployment refusal; a process cannot infer "am I deployed?" from nothing.

**Decision.** Treat a process as **non-local** if any of the following holds:

1. `TASKFENCE_DATABASE_URL` is set and is not a SQLite URL;
2. JWT/Auth0 authentication is enabled;
3. any live connector credential is configured;
4. `TASKFENCE_ENV=deployed`.

If non-local, `TASKFENCE_ENV=deployed` and `TASKFENCE_DATA_MODE` must both be set explicitly; missing or invalid values fail startup. If no non-local signal is present, default `TASKFENCE_ENV=local` and `TASKFENCE_DATA_MODE=synthetic`. Contradictory combinations (`ENV=local` with a non-SQLite DB, JWT auth, or live connector credentials) are rejected at startup.

Gemini gating on top of that:

- `TASKFENCE_GEMINI_BILLING_ENABLED` — operator assertion that the API project has active billing. Not independently verified; not proof of approval.
- `TASKFENCE_GEMINI_DATA_USE_APPROVED` — separate operator assertion that current terms, data-use rules, retention, region, and contractual suitability were reviewed for this workload. Defaults to false.
- `DATA_MODE=real` requires both assertions true, else the provider refuses to start or call. `DATA_MODE=synthetic` may use the free tier by default; the operator is responsible for the fixtures actually being synthetic.

**Consequences.** A developer pointing at a local PostgreSQL instance must set `ENV=deployed` and `DATA_MODE=synthetic` — conservative by design and must be documented so it is not a surprise. The flags are guardrails against accidental use, not verification of provider terms; product copy must say so.

**Verification.** Synthetic mode starts with both flags unset; `real` without billing refuses; `real` with billing but without data-use approval refuses; `real` with both starts; missing/invalid mode outside the local default fails closed; contradictory combos rejected; model or request input can never set the mode.

---

## AD-14 — Free tiers are demo dependencies, for synthetic data

**Decision.** Every third-party free tier (Supabase, Auth0, Upstash, Gemini, Slack) is a development/demo dependency: quotas, pauses, eligibility changes, and outages are expected, and none carries an SLA. Synthetic fixtures only by default. Production, customer, PII, credential, or proprietary content is sent only after current data-use terms, retention, region, and contractual suitability are reviewed and explicitly approved. No service failure may disable authorization, reuse stale security state, or degrade to ALLOW. Vendor facts are `VERIFY` against current official documentation, never hardcoded into the product.

**Verification.** Documentation of quota/pause/region/retention risks per service; a demo path that works with all cloud services absent; no code path that silently substitutes a weaker provider or a local in-memory stand-in for shared state.

---

## AD-15 — Idempotency and external side effects

**Context.** `tools.send_slack` performs a real HTTP POST on the ALLOW path with no key and no claim record. A database transaction cannot make a third-party call atomic.

**Decision.** State-changing tool calls carry a unique idempotency key. A durable claim (approval claim, idempotency record, required audit) commits **before** dispatch; if that write fails, nothing is dispatched. Provider-side idempotency keys are used where the provider supports them. For non-idempotent providers, an ambiguous timeout produces an `unknown_outcome` state — never a blind retry. No transaction is held open while waiting on an external provider.

**Consequences.** The product does not claim universal exactly-once external delivery. Operators get reconciliation instead (AD-16).

**Verification.** Concurrent `allow_once` → exactly one claim, at most one invocation; repeated key → no second invocation; failed claim → no dispatch; ambiguous timeout → unknown outcome recorded, no automatic retry.

---

## AD-16 — Reconciliation has an owner, a permission, and legal transitions (blocking decision)

**Decision.**

- Permission: `taskfence:reconciliation:resolve`, evaluated through the same composition as AD-01.
- Interface: a documented authenticated admin endpoint or CLI. Not a model- or agent-reachable path.
- Legal transitions only:
  - `dispatch_pending → succeeded`
  - `dispatch_pending → failed_confirmed`
  - `dispatch_pending → unknown_outcome`
  - `unknown_outcome → reconciled_succeeded`
  - `unknown_outcome → reconciled_failed`
- Every resolution records actor, authorization basis, prior state, new state, evidence reference, and timestamp in the audit log.
- Reconciliation never automatically re-dispatches. If an operator chooses to redo the work, that is a **new** operation with a new request and a new idempotency key.

**Verification.** Unauthorized actor → denied; illegal transition → rejected; every transition audited; no code path re-dispatches from a reconciliation call.

---

## AD-17 — Kill switch semantics

**Decision.** Kill state (tool, destination, agent identity, or whole gateway) is durable and shared across gateway processes, stored with the control-plane state. A process-local override may supplement it for a single unreachable process, but only in the restrictive direction: it may add a denial, never clear one set durably. Kill decisions happen in the gateway before execution.

**Consequences.** Two processes must agree. A local override that un-kills would be a bypass and is prohibited.

**Verification.** Kill engaged in process A is honoured by independent process B; a local override can only deny; audit records the kill decision and its source.

---

## AD-18 — JWKS lifecycle

**Decision.** Cache keys with a bounded TTL and bounded size; never fetch per request; never cache indefinitely. On an unknown `kid`, permit at most one single-flight, rate-limited refresh. On JWKS outage or an unresolved `kid`, reject the token (fail closed). Accept only the explicitly configured algorithm.

**Verification.** Expired cache refresh; single refresh under concurrent unknown-`kid` requests; outage → 401; `alg:none` and alternate-algorithm tokens rejected; no unbounded growth.

---

## AD-19 — Test tiers, preflight, and CI ownership

**Decision.** Three markers beyond the existing `ollama`: `postgres_integration`, `redis_integration`, `live_integration`. The offline deterministic tier is exactly:

```bash
TASKFENCE_OLLAMA_URL=http://127.0.0.1:1 .venv/bin/pytest -m "not postgres_integration and not redis_integration and not live_integration" -q
```

Integration tiers run through a preflight that exits non-zero when a required endpoint/configuration is missing — a requested tier never silently skips. Ordinary deterministic runs may report integration tests as skipped, with an explicit reason.

**Ownership.** Phase 2 creates the first CI workflow (deterministic tier + disposable PostgreSQL job that fails if unavailable). Phase 7 extends it with a Redis job using a disposable service. Phase 11 may add live connector jobs only when explicitly configured, never with production data.

**Verification.** Missing configuration → non-zero preflight, not a green run; CI Postgres job fails on unavailability; no production credentials in CI logs.

---

## AD-20 — Audit content and retention

**Decision.** Audit records carry event id, timestamp, tenant, user, agent, session, task, contract id/version, policy version, tool + version, action, source resources, destination, classification, lineage reference, decision, reason codes, approval id, request hash/idempotency key, and trace id. Secrets and raw sensitive payloads are never stored. Text fields are minimized to what an investigation needs. A retention and access policy is documented before any real data is used.

**Consequences.** Today `audit.py` stores full `task_text` and `payload[:200]`; both are minimization targets. A denial must remain a denial when audit storage is unavailable; the audit failure is reported separately and never converts to ALLOW.

**Verification.** Sensitive-string absence test against normal audit payloads; audit-write failure never yields ALLOW; append-only API surface (no update/delete).

---

## AD-21 — One configuration source of truth

**Context.** Environment reads are scattered across seven modules and `TASKFENCE_DB` is honoured only by the dashboard, so gateway and dashboard can read different databases.

**Decision.** Phase 1 introduces a single configuration module with safe local defaults, a `.env.example`, secret-free representation, early validation, and refusal to run in an inconsistent state. The database path/URL has exactly one resolution path shared by gateway, dashboard, and tests. JWT mode must never silently downgrade to anonymous.

**Verification.** Defaults; overrides; JWT-mode requirements; secrets absent from `repr`/logs; invalid configuration fails at startup; a single DB-path resolution test covering gateway and dashboard.

**Database precedence (resolved 2026-10-17, before Phase 2 code).** A PostgreSQL URL and a non-default SQLite path must not both be configured:

- URL set + `TASKFENCE_DB` unset or set to the default `taskfence.sqlite3` → PostgreSQL is authoritative; no SQLite file is read by a security path.
- URL unset → SQLite at `TASKFENCE_DB` (or the default).
- URL set + `TASKFENCE_DB` set to any other path → startup `ConfigError`. No silent precedence, no warning-and-continue.

Normalization for the comparison is `os.path.normpath(os.path.expanduser(value))` with no symlink resolution, so lexical equivalents of the default (`./taskfence.sqlite3`, `sub/../taskfence.sqlite3`) are accepted alongside a URL while an absolute, `~`-expanded, symlinked, or differently-cased path is a conflict.

**Human decision (2026-10-17):** the reviewer accepted this rule as the documented safe rule and requires every case to be tested, including path normalization and the conflicting-backend case. This closes the precedence question. Rationale for allowing the default value: `.env.example` ships it active, and copying it while adding a URL is the expected migration path; a *different* SQLite path is what signals two intended backends. Phase 2 implements the rule and the conflict/valid matrix in `docs/implementation/PHASE_2_READINESS.md` §2.

---

## AD-22 — Claims discipline

**Decision.** Every security claim in product copy must correspond to a currently passing test. The banned-claims lint (`tests/test_copy_claims.py`) continues to apply to reworded copy: the bar does not move because the audience changed. Reasons → commands, not adjectives. Live integrations are named exactly as exercised. Production readiness and universal attack prevention are never claimed.

**Verification.** Copy-claims test passes; every quoted number matches a fresh canonical run; the ASYNC'26 origin appears once as history.

---

## AD-23 — OAuth lifecycle for real connectors

**Context.** Phase 11 introduces live providers (Slack, Drive, GitHub) whose credentials are per-user, scoped, expiring, and revocable — a different shape from the environment-backed webhook in `taskfence/tools.py`.

**Decision.** Every live connector must satisfy all of the following before it is enabled:

- **Least privilege:** request only the scopes the registered operation needs; no broad or wildcard scopes.
- **Per-user consent:** the authenticated principal grants access; consent is never inherited from another user or from an operator-wide token.
- **Principal/tenant binding:** each token is bound to the verified principal and tenant, and to the approved provider resources (channel, folder, repository). A token is never reusable across users, and a resource check happens on every call, not only at connect time.
- **Expiry and refresh:** expiry is tracked; refresh happens proactively, and a failed refresh produces a deny — never a fallback to a wider credential or to ALLOW.
- **Revocation:** a revoked or invalidated token fails closed and requires reconnection.
- **Reconnect-required state:** surfaced explicitly to the operator; it is a state, not an error to retry blindly.
- **Secure storage:** refresh tokens live only in an approved secret store or encrypted persistence design. If that storage is not implemented, the connector stays disabled and the deterministic fake path remains.
- **No leakage:** access and refresh tokens never appear in agent/MCP arguments, prompts, logs, audit events, approval previews, dashboard state, or connector error strings.

**Consequences.** Phase 6 must define the interface fields now (scopes, expiry, refresh, revocation, provider/resource binding) even though its first implementation is environment-backed. Phase 11 cannot be declared complete on mock tests alone for these properties.

**Verification.** Scope/consent/expiry/refresh/revocation tests per connector; principal-binding and cross-user-reuse denial; expired token never bypasses authorization; no token material in any audited or agent-visible surface; connector disabled when secure storage is absent.

---

## AD-24 — Latency baseline and performance claims

**Status:** `VERIFY` — no measurement exists in this tree.

**Decision.** Measure p50/p95 for a local deterministic ALLOW and a local deterministic BLOCK path on the same corpus, before and after instrumentation. Report the numbers, and set a project-specific target with the human reviewer. No universal target is asserted, and no speed claim is made beyond what was measured.

**Verification.** Recorded before/after results committed in Phase 9 output; Phase 13 re-measures and compares against the documented target.

---

## AD-25 — Phase 2 authoritative-state rules

**Status:** Accepted design; not implemented.

**Context.** G-01 (process-local task state), G-02 (in-memory taint), G-03 (non-atomic approval transition) and G-16 (backend divergence) must be closed together, because each one hides behind the same in-memory shortcut.

**Decision.**

1. **Authority split.** `tasks`, `task_contracts`, and `task_decisions` are authoritative. `task_state` remains a dashboard projection and no decision path reads it. Module-level dicts may exist only as reconstructible caches validated against the stored contract version.

   **Append-only decision history — human decision (2026-10-17):** `task_decisions` is append-only from the initial Phase 2 migration. Every authorization decision creates a distinct historical record; earlier records are never overwritten. Latest/current views are derived (ordered query or same-transaction projection), never mutable authority. Implementation stays minimal: one table, explicit SQL, no triggers; the write surface exposes append and read only, and a test must show that normal application write paths cannot mutate a prior decision record.
2. **Taint is durable and monotonic.** `task_reads (task_id, asset_id)` is append-only, written in the same transaction as the lineage node, and union-only. No path may reduce an effective label set after a restart; retention is a Phase 10 policy decision.
3. **Approval resolution is a conditional claim.** A single `UPDATE … SET status='allowed_once' WHERE approval_id=? AND status='pending'` is the claim: `rowcount == 1` owns execution, `rowcount == 0` is a fail-closed 409 with nothing executed. The claim commits before dispatch; a failed execution never returns the record to `pending`.
4. **One backend.** A single `Persistence` factory built from `Config` serves gateway, dashboard, scenarios, and tests. No security path may construct a store with a hardcoded SQLite path.

**Consequences.** Every decision path gains a durable read, which is why AD-05 matters. Concurrency guarantees are accepted only from PostgreSQL (AD-04); SQLite demonstrates demo parity only.

**Verification.** `docs/implementation/PHASE_2_READINESS.md` §3–§4 defines the required tests, including the two-interpreter cross-process test and its negative control.
