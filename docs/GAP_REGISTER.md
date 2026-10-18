# TaskFence — Gap Register

**Phase:** -1 (design-only artifact)
**Status:** Live register. Updated at every phase gate.
**Evidence snapshot:** commit `bf6418f`. Line numbers are valid for that commit only.

## Rules for this register

1. A row may be marked `CLOSED` **only** with a test or file reference that proves it. "Implemented" is not evidence.
2. `VERIFY` means the fact could not be established from the checked-out tree and the owning phase must confirm it before relying on it.
3. `BLOCKED` means an external decision, account, or credential is required.
4. Every phase gate updates the rows it touched. Phase 13 audits the whole register.
5. Severity: `Critical` (defeats the product's security claim), `High` (material security gap), `Medium` (correctness, operability, or honesty), `Low` (hygiene).
6. Scope: `B` = blocking for the phase that owns it; `A` = advisory, may proceed with an explicit owner and acceptance.

## Status summary (updated at Phase 2)

| Status | Count |
|---|---|
| OPEN | 21 |
| VERIFY | 2 |
| BLOCKED | 0 |
| CLOSED | 7 |
| **Total rows** | **30** (G-01 … G-30) |

**Phase 0 baseline recorded (2026-10-17, commit `bf6418f`):** deterministic tier `272 passed, 1 skipped, 1 deselected`, exit 0; `git diff --check` exit 0. No gap was closed, because no implementation changed. Evidence was refined on G-01, G-02, G-03, G-16, G-17 and G-19; G-29 was added. Details in `docs/BASELINE.md`.

**AD-05 accepted (2026-10-17).** The human reviewer approved the fail-closed protected-action availability policy with two clarifications: denial is scoped to the protected action (not a whole-application crash), and optional telemetry/dashboard failures may occur independently as long as the security decision and required audit guarantees hold. This closes the Phase -1 decision gate; it did **not** close any implementation gap. Enforcement is tracked by G-30, and the acceptance record lives in `docs/ARCHITECTURE_DECISIONS.md` AD-05.

**Phase 1 recorded (2026-10-17).** `taskfence/config.py` (validated, secret-masking configuration), `.env.example`, and `tests/test_config.py` (19 tests) exist. Full deterministic tier: `291 passed, 1 skipped, 1 deselected`, exit 0. No gap was closed: the module is not yet adopted by existing call sites, because the pack assigns persistence wiring to Phase 2 and each concern's adoption to its owning phase. G-22 and G-16 remain OPEN with evidence updated.

**Phase 2 readiness recorded (2026-10-17), documentation only.** `docs/implementation/PHASE_2_READINESS.md` resolves database precedence (reject URL + non-default SQLite path), the authoritative-versus-projection split, durable monotonic taint, the conditional-UPDATE approval claim, and the PostgreSQL cross-process acceptance tests. No schema, dependency, or application code changed. **No gap was closed:** G-01, G-02, G-03 and G-16 stay OPEN with design references added to their required-test columns.

**Phase 2 decisions accepted (2026-10-17), documentation only.** The reviewer accepted the database precedence rule, requiring every case to be tested including path normalization and the conflicting-backend case, and decided that `task_decisions` is append-only from the initial Phase 2 migration, with derived latest views and a test proving normal application write paths cannot mutate prior records. Recorded in AD-21, AD-25 and `PHASE_2_READINESS.md` §2.1.1/§3.1.1. **No gap status changed;** G-01's required test now also names decision-history immutability.

**Phase 2 implemented (2026-10-17).** `taskfence/persistence.py` (one `Persistence` factory from `Config`; versioned forward-only schema; explicit SQL; append-only `task_decisions` with a monotonic `seq`; conditional-UPDATE approval claim; append-only `task_reads`; AD-03 reserved columns). `taskfence/config.py` precedence matrix + relative-path anchoring. Gateway, audit, approvals, lineage, dashboard and the scenario harness all build their stores from that single configured backend. New tests: `tests/test_persistence_decisions.py`, `tests/test_cross_process.py`, `tests/test_approval_concurrency.py`, `tests/test_backend_unification.py`, `tests/test_postgres_integration.py`, helpers `tests/proc_child.py` / `tests/child_helpers.py`, preflight `scripts/postgres_preflight.py`, CI `.github/workflows/tests.yml`. Commands run: `.venv/bin/python -m pytest -q` → `322 passed, 11 deselected`, exit 0; `TASKFENCE_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/taskfence_test .venv/bin/python -m pytest -m postgres_integration -q` → `9 passed, 324 deselected`, exit 0 (PostgreSQL 16.2); `python scripts/postgres_preflight.py` → exit 2 unset / 3 unreachable / 0 reachable; `git diff --check` exit 0. **Closed with test evidence:** G-01, G-02, G-03, G-16, G-17, G-23. **Not closed:** G-18 (workflow written, not executed in this environment). No commit or push was made.

**Phase 2 review follow-up (2026-10-18), implementation + tests.** F1–F4 from the Phase 2 review: `expand_task` added to the decision-immutability test; post-restart `expand_task` restored to fail closed (live review session is process state, not persisted); the destructive demo reset is confirmation-gated and documented as an operator action; `ApprovalStore.set_status` removed, `claim()` restricted to resolution outcomes. A read-only approval-lifecycle review then produced four scoped fixes: **R1** current policy is re-evaluated on replay and a current hard violation can never be overridden by an earlier human approval; **R1b** the reviewed tool identity/action/destination and the connector configuration are revalidated pre-claim; **R3** an approved read is bound to the asset content digest and classification captured at review; **R2** the dashboard pending card shows the creation timestamp and age (no expiry is enforced). New tests: `tests/test_approval_drift.py`, plus additions to `tests/test_approvals.py`, `tests/test_approval_persistence.py`, `tests/test_persistence_decisions.py`, `tests/test_dashboard_smoke.py`. Commands run: `.venv/bin/python -m pytest -q` → `341 passed, 11 deselected`, exit 0; `pytest -m postgres_integration -q` → `9 passed, 343 deselected`, exit 0 (PostgreSQL 16.2, preflight exit 0); `git diff --check` exit 0. **Closed with test evidence:** G-28. **Unchanged:** G-18 stays VERIFY (workflow still not executed); no expiry duration or new policy was invented. No commit or push was made.

No blocking **design decision** remains open: availability posture, the local/deployed discriminator, reconciliation transitions, budget ownership, and dialect policy are decided in `docs/ARCHITECTURE_DECISIONS.md`. The rows below are implementation work with named owners — they are expected to be open today.

---

## Register

| ID | Sev | B/A | Gap | Evidence (current tree) | Owner | Required test / evidence | Fail-closed behavior | Status |
|---|---|---|---|---|---|---|---|---|
| G-01 | Critical | B | `CONTRACTS` / `TASK_TEXT` / `LAST_DECISION` were process-local authoritative state; a contract created in one worker was invisible to a tool call resolved in another | Phase 2 removed in-memory authority: `taskfence/persistence.py` (`tasks` / `task_contracts` / `task_decisions`), `taskfence/gateway.py` builds `TASKS` from the single `Persistence` factory and 404s on a missing durable task row; `tests/test_cross_process.py`, `tests/test_persistence_decisions.py`, `tests/test_postgres_integration.py` | Phase 2 | CLOSED with: two-interpreter test + 404 negative control (`tests/test_cross_process.py::test_contract_created_by_one_interpreter_is_usable_by_another`, `::test_negative_control_unknown_task_is_404_in_a_fresh_interpreter`), append-only history immutability (`tests/test_persistence_decisions.py`), PostgreSQL equivalents in `tests/test_postgres_integration.py` | Deny (404) from a durable miss, never from process memory | CLOSED |
| G-02 | Critical | B | Conservative session taint (`session_reads`) was in-memory only; a restart silently dropped restrictions the task had already accumulated | `taskfence/lineage.py` now records reads in the append-only `task_reads` table in the same transaction as the lineage node; `session_asset_ids()` reads the durable table, so a fresh process sees the same taint | Phase 2 | CLOSED with: restart test + cross-process taint test (`tests/test_cross_process.py::test_taint_survives_restart_and_cross_process_execution`, `::test_taint_survives_an_in_process_restart_of_the_gateway`), PostgreSQL taint-union test `tests/test_postgres_integration.py::test_concurrent_taint_registration_preserves_the_union` | A restart can only preserve or add restriction, never remove it | CLOSED |
| G-03 | High | B | Approval status transition was not atomic: the gateway checked `status != pending`, then `set_status` updated with `WHERE approval_id = ?` only | `taskfence/approvals.py:claim()` is a single conditional UPDATE (`WHERE approval_id = ? AND status = 'pending'`); `rowcount == 0` returns 409 and executes nothing; the claim commits before the tool runs | Phase 10 (row) / Phase 2 (implementation) | CLOSED with: eight independent interpreters on PostgreSQL (`tests/test_postgres_integration.py::test_concurrent_approval_resolution_has_exactly_one_winner`: one 200, seven 409, exactly one outbox line) and the SQLite parity end-to-end race (`tests/test_approval_concurrency.py`); the Phase 10 unknown-outcome remainder stays with G-11/G-24 | Second resolver fails closed with 409; nothing executes | CLOSED |
| G-04 | Critical | B | No user authentication; only a shared `X-Admin-Token` gates approval detail and resolution | `taskfence/gateway.py:302-309,377,548` | Phase 3 | JWT matrix: valid/expired/wrong iss/wrong aud/bad sig/`alg:none`/missing → 401 | 401, never anonymous in JWT mode | OPEN |
| G-05 | Critical | B | No principal authorization layer; the task contract is the only gate, so a contract can widen what a user may move | `taskfence/policy.py` (task-policy only); no principal model | Phase 4 | User∩agent∩policy matrix incl. task-expansion-denied and unknown-principal cases | Deny | OPEN |
| G-06 | High | B | Destination/resource granularity is coarse: one `sales_slack` destination, no channel/repo/folder identity, no URL host validation | `taskfence/catalog.py`, `taskfence/registry.py`, `taskfence/tools.py` | Phase 5 | #sales vs #finance, approved vendor vs lookalike host, unknown resource → BLOCK | Deny | OPEN |
| G-07 | High | B | No credential broker; the provider webhook is read directly from process env by tool code | `taskfence/tools.py` (`real_slack_webhook`) | Phase 6 | BLOCK/APPROVE resolve zero credentials; ALLOW resolves exactly once; exception strings contain no secret | Credential unavailable → deny | OPEN |
| G-08 | High | A | MCP subprocess environment inheritance is untested; provider/database secrets must not be inherited | `mcp_gateway/server.py`, `scenarios/mcp_runner.py:147` | Phase 6 | Env-allowlist test on a launched MCP subprocess | Refuse to launch / refuse call | OPEN |
| G-09 | Critical | B | No budgets or rate limits; only an agent-side `MAX_STEPS = 8` that a replaced agent could ignore | `taskfence/agent.py` (`MAX_STEPS`); no gateway accounting | Phase 7 | Below/at/over limit, replay, concurrency, atomic increment+expiry | Deny on exhausted or unavailable shared accounting | OPEN |
| G-10 | High | B | No cloud model path; only Ollama/Qwen3 | `taskfence/agent.py`, `taskfence/contract.py` | Phase 8 | Provider normalization + function-call parsing + same-gateway assertions | Malformed/unknown → gateway deny | OPEN |
| G-11 | High | B | External side effect (real Slack POST) has no idempotency key, no durable claim, no unknown-outcome state | `taskfence/tools.py:_post_real_slack` | Phase 10/11 | Repeated key → no second invocation; ambiguous timeout → unknown outcome, no blind retry | Do not dispatch without a durable claim | OPEN |
| G-12 | Medium | A | No distributed tracing; no correlation across model → gateway → tool | no OTel imports anywhere | Phase 9 | trace_id/task_id/decision survive model→gateway→tool; denied flows traced | Telemetry failure must not alter a decision | OPEN |
| G-13 | High | B | Audit schema lacks the Phase 10 field set (tenant, user, agent, session, policy version, idempotency key, trace_id, approval actor) | `taskfence/audit.py` `_SCHEMA` (14 columns) | Phase 10 | Field-level audit assertions per security outcome | Audit write failure must not convert a denial to ALLOW | OPEN |
| G-14 | High | B | No kill switch | absent everywhere | Phase 10 | Kill tool/destination/agent; second process honors it | Deny before execution | OPEN |
| G-15 | Medium | B | No health/readiness endpoints; no startup config validation beyond ad-hoc env reads | `taskfence/gateway.py` (no health route) | Phase 10 | Readiness reflects DB/kill-switch/JWKS availability | Not ready ⇒ protected actions denied | OPEN |
| G-16 | Medium | B | `TASKFENCE_DB` was honored by the dashboard and tests but not by gateway stores, which hardcoded `taskfence.sqlite3` | One `Persistence` factory (`taskfence/persistence.py`) builds every store from one `Config`; the dashboard resolves the same backend through `_backend()`; relative SQLite paths are anchored to the project root so the working directory cannot change the file | Phase 1 (accessor) / Phase 2 (wire stores + dashboard) | CLOSED with: the precedence/conflict matrix in `tests/test_config.py` (11 cases incl. normalization and the conflicting backend), the same-backend test from a different working directory (`tests/test_backend_unification.py::test_gateway_and_dashboard_resolve_the_same_backend`), the no-hardcoded-path scan, and `tests/test_postgres_integration.py::test_postgres_configuration_never_falls_back_to_sqlite` | n/a (correctness); a configured PostgreSQL backend never falls back to SQLite | CLOSED |
| G-17 | Medium | B | No `postgres_integration` / `redis_integration` markers or preflight; a requested-but-missing tier could silently pass | `pytest.ini` registers `postgres_integration` and deselects it by default; `scripts/postgres_preflight.py` gates the tier; `tests/test_postgres_integration.py` calls `pytest.fail()` when `TASKFENCE_TEST_DATABASE_URL` is absent | Phase 2 | CLOSED with: preflight exit 2 (unset) / 3 (unreachable) / 0 (reachable) observed in the Phase 2 run, and the runtime `pytest.fail()` in the tier fixture (a missing tier fails, never skips) | Fail preflight, never a silent skip | CLOSED |
| G-18 | Medium | B | No CI workflow | `.github/workflows/tests.yml` written in Phase 2: deterministic job plus a disposable PostgreSQL 16 service that runs the preflight and the `postgres_integration` tier | Phase 2 | VERIFY: the workflow exists but has not been executed here (no GitHub runner in this environment); the same commands pass locally on both backends | CI fails when the database is unavailable; it never skips | VERIFY |
| G-19 | Low | A | No latency **target**; baseline existed only as a VERIFY placeholder before Phase 0 | Phase 0 preliminary baseline in `docs/BASELINE.md` §6 (ALLOW p50 3.433 ms / p95 3.969 ms; BLOCK p50 3.365 ms / p95 3.794 ms, in-process TestClient, n=200) | Phase 9 | p50/p95 ALLOW and BLOCK, before/after instrumentation | n/a | OPEN |
| G-20 | Medium | A | Upstash atomicity unverified; no Redis dependency or `/eval` usage | none | Phase 7 | Single Lua check+dedupe+increment+expiry against a real test Redis | Shared accounting unavailable → deny | VERIFY |
| G-21 | High | B | No local/deployed discriminator; nothing prevents a real-data deployment from using a free-tier model | none | Phase 8 | ENV/DATA_MODE matrix incl. contradictory combinations | Refuse startup | OPEN |
| G-22 | Medium | B | Configuration still scattered: the validated module exists, but existing call sites read `os.environ` directly and bypass it | `os.environ` reads remain in `contract.py`, `agent.py`, `client.py`, `gateway.py`, `tools.py`, `mcp_gateway/server.py`, `dashboard/app.py`; new `taskfence/config.py`, `.env.example`, `tests/test_config.py` (19 tests) cover defaults/overrides/secret masking/early failure | Phase 1 (module) / later phases (adopt per concern) | Adoption test per module: no direct `os.environ` read for a configured key remains | Invalid config ⇒ fail startup | OPEN |
| G-23 | High | B | Schema had no reserved columns for identity, tenant, agent, policy version, trace, idempotency, approval actor, resource ids, kill switch | `taskfence/persistence.py::_RESERVED_COLUMNS` designs the full column set up front (nullable, with the owning phase documented) and adds it forward-only; versioned migrations are idempotent on both dialects | Phase 2 | CLOSED with: legacy-upgrade test that preserves rows (`tests/test_backend_unification.py::test_reserved_columns_exist_and_migrations_are_forward_only`) and the PostgreSQL migration test (`tests/test_postgres_integration.py::test_migration_is_idempotent_and_preserves_existing_rows`) | n/a | CLOSED |
| G-24 | High | B | No reconciliation surface for unknown external outcomes (role, permission, transitions, audit) | none | Phase 10 | Unauthorized actor denied; only legal transitions; no auto-redispatch | Unknown outcome stays unknown until reconciled | OPEN |
| G-25 | Medium | B | Audit stores raw `task_text` and truncated payloads with no minimization or retention policy | `taskfence/gateway.py:_audit`, `_finish` | Phase 10 | Sensitive-string absence test against normal audit payloads | n/a | OPEN |
| G-26 | Medium | A | Local/demo mode exercises task policy only; no principal-level denial is demonstrable | `taskfence/gateway.py`, scenarios A–G | Phase 4/12 | Explicit local principal permission set + one honest principal-denial case | Deny | OPEN |
| G-27 | Medium | A | Product copy is hackathon-framed (`docs/JUDGE_QA.md`, `docs/DEMO_SCRIPT.md`); no rename or reframing | `docs/`, `README.md` | Phase 12 | Copy-claims test passes; fresh counts; no stale references | n/a | OPEN |
| G-28 | Low | A | `approvals.set_status` returns rowcount but callers never check it; a lost update is silent | `taskfence/approvals.py` now exposes no unconditional status setter at all: `claim()` is the only transition, is conditional on `status = 'pending'`, accepts only a resolution outcome (`RESOLUTION_STATUSES`) and returns `False` for every loser; the gateway turns `False` into 409 before executing anything | Phase 10 (row) / Phase 2 (implementation) | CLOSED with: `tests/test_approvals.py::test_resolution_cannot_bypass_the_pending_state_claim` (no `set_status`, invalid target raises before a write, later claims false), the 8-process SQLite race `tests/test_approval_concurrency.py::test_eight_independent_processes_resolve_one_approval` and its PostgreSQL equivalent `tests/test_postgres_integration.py::test_concurrent_approval_resolution_has_exactly_one_winner` | Lost claim is treated as a failed resolution (409), never a silent override | CLOSED |
| G-30 | High | B | The approved AD-05 fail-closed policy has no enforcement or test: no protected action currently distinguishes "dependency unavailable" from a decision, and there is no proof that denial is scoped to the protected action rather than crashing or degrading silently | No availability logic anywhere; `gateway.py` has no health/readiness route; no test simulates an unreadable authoritative store | Phase 10 | For each protected operation with its authoritative dependency unavailable: outcome is BLOCK/DENY, no stale-ALLOW path, and non-security endpoints still respond; second process agrees | Deny the protected action; report the dependency failure separately; never convert to ALLOW | OPEN |
| G-29 | Medium | A | `requirements.txt` has no version pins, so the environment is reproducible in shape but not by version; Phase 0 installed floating latest (e.g. fastapi 0.143.3, pydantic 2.14.1, pytest 9.1.1) | `requirements.txt`; Phase 0 `pip freeze` in `docs/BASELINE.md` §3 | Phase 1 | Documented pinned/tested versions or a lock file; deterministic tier passes against them | n/a | OPEN |

---

## Blocking checklist by phase

A phase may not be declared complete while a blocking row it owns is still `OPEN`. Rows marked `A` may proceed with an explicit owner and acceptance recorded at the gate.

**Documented correction (2026-10-17, Phase 1).** The Phase -1 checklist marked G-22 and G-16 as fully blocking for Phase 1. That was too strict: the pack's Phase 1 explicitly excludes persistence migration and assigns each concern's adoption to its owning phase (Phase 2 persistence, Phase 3 identity, Phase 6 credentials, Phase 8 providers). The Phase 1 obligation is therefore the accessor, the template, and their tests; consumer adoption stays tracked against the owning phases. No evidence was removed and no gap was closed by this correction.

| Phase | Blocking rows |
|---|---|
| -1 | none remaining — decisions recorded in `docs/ARCHITECTURE_DECISIONS.md` (AD-05, AD-13, AD-16) |
| 0 | none (baseline only); must record evidence for G-01 and G-02 |
| 1 | G-22 (module + template + validation portion — done); G-16 accessor (done); full closure of both belongs to the phases that adopt them |
| 2 | G-18 only (VERIFY — workflow written, not executed); G-01, G-02, G-03, G-16, G-17, G-23 and G-28 closed with test evidence |
| 3 | G-04 |
| 4 | G-05 |
| 5 | G-06 |
| 6 | G-07 (G-08 advisory) |
| 7 | G-09 |
| 8 | G-10, G-21 |
| 9 | none blocking (G-12, G-19 advisory) |
| 10 | G-11, G-13, G-14, G-15, G-24, G-25, G-30 (G-03's atomic-claim portion closed in Phase 2 with PostgreSQL evidence; unknown-outcome/reconciliation stays with G-11/G-24) |
| 11 | G-11 (connector half) |
| 12 | G-27 |
| 13 | full register review |

---

## Notes on the three highest-severity rows

### G-01 — process-local authoritative task state (confirmed, with a refinement)

The inherited prompt-pack description is **correct for this tree**, but more precise than "zero persistence":

- `TaskStateStore` (`taskfence/audit.py:104+`) does persist a `task_state` snapshot, and `gateway._save_state` writes it after each decision — but it is documented and used as **display data for the dashboard only**.
- The decision input is `CONTRACTS[task_id]` (`gateway.py:289`). After a restart the dict is empty, `tool_call` returns 404, and `expand_task` fails closed with "task is not active in this gateway process" (`gateway.py`, expand_task branch).
- Consequence: with more than one worker, a legitimate contract is unusable in every process that did not create it.

Phase 2 must move the authoritative state and keep `task_state` as a projection, not promote the projection to authority without verifying that it is complete and current.

### G-02 — session taint is security-relevant and lost on restart

`LineageTracker.session_reads` drives conservative-mode label union (`effective_labels`, `lineage.py:141`) and therefore the outbound decision. Because it starts empty on every construction (`lineage.py:41`), a restart makes the gateway **more permissive** than before for the same task. This is the opposite direction from the fail-closed rule and must be closed in the same phase as G-01, with a restart test.

### G-03 — approval race

Sequential replay is well covered (`tests/test_approval_persistence.py:214`, `tests/test_approval_integrity.py`). Concurrency is not: `set_status` has no `WHERE status = 'pending'` guard and no `FOR UPDATE`, so two resolvers that both read `pending` can both proceed. SQLite's file locking makes this unlikely to surface in single-threaded tests, which is precisely why the Phase 10 test must be written against PostgreSQL and use real concurrency.

---

## Phase-by-phase update rule

At every phase gate:

1. Update the rows whose owner phase is the one that just ran.
2. `CLOSED` requires a named test and the file that implements it.
3. If a phase finds a new gap, add a row with severity, owner phase, required test, and fail-closed behaviour.
4. Never delete a row. Superseded rows are marked `CLOSED` with the superseding reference.
5. Phase 13 must produce a full register review: every row `CLOSED` with evidence, or explicitly `OPEN`/`BLOCKED`/`VERIFY` with owner and reason.
