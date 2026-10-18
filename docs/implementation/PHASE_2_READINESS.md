# Phase 2 Readiness — Persistence Design (documentation only)

**Status:** readiness record, updated with the Phase 2 implementation outcome (see §6).
**Prepared:** 2026-10-17, after Phase 1 acceptance and before any Phase 2 code.
**Binding inputs:** `AD-02` (durable authority), `AD-03` (schema reservation), `AD-04` (SQL dialects), `AD-05` (fail-closed, human-approved), `AD-19` (test tiers), `AD-21` (configuration), `AD-25` (Phase 2 rules).
**Gaps covered:** G-01, G-02, G-03, G-16.

---

## 1. Scope

This document resolves the four design questions that must be settled *before* Phase 2 writes any persistence code:

1. Which configuration wins when both a PostgreSQL URL and a SQLite path are supplied.
2. Which state is authoritative and which is a projection, per gap.
3. How lineage taint survives restart and crosses processes.
4. What counts as acceptable evidence for PostgreSQL concurrency, including a real cross-process test.

Out of scope for Phase 2: identity (Phase 3), authorization (Phase 4), resource granularity (Phase 5), credentials (Phase 6), budgets (Phase 7), cloud models (Phase 8), telemetry (Phase 9), kill switch / reconciliation / audit hardening (Phase 10), connectors (Phase 11).

---

## 2. Database configuration precedence (resolves the Phase 1 open question)

### 2.1 Rule

| `TASKFENCE_DATABASE_URL` | `TASKFENCE_DB` | Outcome |
|---|---|---|
| unset / empty | unset / empty | SQLite at the default `taskfence.sqlite3` |
| unset / empty | any path | SQLite at that path |
| set (PostgreSQL URL) | unset / empty | **PostgreSQL is authoritative**; no SQLite file is created or read by security paths |
| set (PostgreSQL URL) | set to the default `taskfence.sqlite3` | **PostgreSQL is authoritative**; the default SQLite path is treated as "not configured" and must not be read by a security path |
| set (PostgreSQL URL) | set to **any other path** | **Startup error** — ambiguous persistence backend |

Conflict detection is deliberately conservative: the comparison is exact after `Path` normalization, so a path that merely *resolves* to the same file is still a conflict. Silently preferring one backend is exactly how a gateway and dashboard end up disagreeing about security state (G-16).

Rationale for allowing the default path alongside a URL: `.env.example` ships `TASKFENCE_DB=taskfence.sqlite3` active, and copying it while adding a database URL is the expected migration path. Rejecting that combination would fail a correct configuration. A *different* SQLite path is what signals two intended backends.

### 2.1.1 Human decision (2026-10-17)

The reviewer **accepted** this precedence rule as the documented safe rule, with every case to be tested — including path normalization and the conflicting-backend case. This closes the precedence question; Phase 2 implements it, not re-derives it.

**Normalization used for the comparison:** `os.path.normpath(os.path.expanduser(value))`, with no symlink resolution. Consequences:

- `taskfence.sqlite3`, `./taskfence.sqlite3`, and `sub/../taskfence.sqlite3` are all equivalent to the default → accepted alongside a URL (PostgreSQL authoritative).
- An absolute path, a `~`-expanded path that differs, or any symlinked path that differs is a conflict, even if it happens to address the same file. Conservative by design: only lexical equivalence counts.
- Comparison is exact on the running platform; on a case-insensitive filesystem two differently-cased paths are still treated as a conflict.

### 2.2 Error behaviour

- Raised during configuration construction, before the application serves any request (`ConfigError`), never at first use.
- Message names both variables and states the fix: unset `TASKFENCE_DB` or remove the URL.
- No warning-and-continue path. No "URL wins silently" path.

### 2.3 Required tests (written when Phase 2 implementation begins)

| Case | Expected |
|---|---|
| URL only | `use_postgres is True`; SQLite path recorded but unused |
| URL + default `TASKFENCE_DB` | accepted; PostgreSQL authoritative |
| URL + non-default `TASKFENCE_DB` (conflicting-backend case) | `ConfigError` at startup |
| DB path only | SQLite authoritative; no PostgreSQL attempt |
| Neither | default SQLite path |
| URL of a non-PostgreSQL scheme | `ConfigError` (already implemented in Phase 1) |
| URL + `./taskfence.sqlite3` | accepted (normalizes to default) |
| URL + `sub/../taskfence.sqlite3` | accepted (normalizes to default) |
| URL + absolute path to a `taskfence.sqlite3` | `ConfigError` (different path identity) |
| URL + `~`-expanded path | `ConfigError` unless it normalizes to the default |
| URL + path differing only by case on a case-insensitive FS | `ConfigError` |

Also required: a test asserting the gateway and the dashboard resolve the **same** backend from the same configuration snapshot (G-16), including the case where the dashboard is started in a different working directory.

---

## 3. Authoritative state versus display projections

### 3.1 G-01 — task, contract, and last-decision state

**Authoritative (new durable tables, Phase 2):**

| Table | Key | Contents |
|---|---|---|
| `tasks` | `task_id` | task text, created timestamp, authenticated subject (reserved, populated in Phase 3) |
| `task_contracts` | `(task_id, contract_version)` | frozen contract snapshot; current pointer in `tasks.current_contract_version` |
| `task_decisions` | `decision_id` | append-only decision records with contract version, outcome, reasons; "last decision" is a query, not a mutable column |

**Projection, never authority:** `task_state` (existing `TaskStateStore`) remains a dashboard snapshot. It may lag, and no decision path may read it.

### 3.1.1 Append-only decision history — human decision (2026-10-17)

`task_decisions` is **append-only from the initial Phase 2 migration**. This is a recorded reviewer decision, not an implementation preference.

Rules:

- Every authorization decision creates a **distinct historical record**: `ALLOW`, `APPROVE`, `BLOCK`, the allow-but-taint downgrade, and the executed outcome of an approved request. Later decisions never overwrite earlier ones.
- Fields: `decision_id` (primary key), `task_id`, `contract_version`, `tool`, `action`, `destination`, `outcome`, `reasons_json`, `request_fingerprint`, `created_at`. Additional Phase 10 fields are reserved via AD-03, not added now.
- The application write surface exposes **append and read only** — no update, no delete — following the same discipline as `AuditLog`.
- "Last decision" is derived: either `ORDER BY decision_id DESC LIMIT 1` for the task, or a projection maintained in the same transaction as the append. The projection is never authoritative and may be rebuilt.
- Minimal implementation: one table, explicit SQL, no triggers, no ORM. Immutability is guaranteed by the **application API surface** and verified by tests — never described as database-enforced immutability.
- Retention, redaction, and pruning are Phase 10 policy decisions. Until then nothing expires in normal operation. The only path that removes decision rows is the dashboard's destructive demo/test reset (§3.5): an explicit, operator-confirmed action, not an application runtime path.

Required tests (Phase 2):

1. Append N decisions for one task; every earlier row is unchanged field-for-field afterwards.
2. A normal application path — tool call, approval resolution, `expand_task` — cannot mutate a prior decision record.
3. The module exposing decision writes has no update/delete method (inspection test, same pattern as the audit tests).
4. The derived latest view equals the most recently appended decision after a restart and from a second process.

**Decision-path rule:** `GET /tasks/{id}` and `POST /tasks/{id}/tool-call` load the contract from `task_contracts` using the pointer. A missing row is a 404, exactly as today, but for a durable reason rather than process memory.

**Contract versioning:** `expand_task` inserts a new `task_contracts` row and advances the pointer in one transaction. Existing versions are never mutated (I3). Approvals keep binding to the exact reviewed version.

**Expansion is a live-session operation (F2):** it requires the durable task row **and** a review session in the serving process (that process created the task). After a restart, or in a different worker, `expand_task` fails closed with 409, nothing executes, and the approval stays `pending` for `deny` / `allow_once`. Restoring the pre-Phase-2 fail-closed rule is deliberate: no expiry or re-authorization policy exists yet, so a stale pending approval must never be able to widen scope indefinitely. A complete expiry/re-authorization design remains with Phase 10 (G-11/G-24).

**No module-level authority:** after Phase 2, `CONTRACTS`, `TASK_TEXT`, and `LAST_DECISION` may survive only as caches that are reconstructible from the database. Any cache entry must be validated against the stored contract version before use; a cache miss or version mismatch must fall back to a database read, never to a stale decision.

**Acceptance:** the cross-process test in §4.3 proves that a contract created by one interpreter is usable by another, and the negative control proves the second interpreter is not sharing memory.

### 3.2 G-02 — lineage taint must be durable and monotonic

**Problem:** `session_reads` is process memory. A restart currently makes the gateway *more permissive* for the same task, which contradicts AD-05's direction of failure.

**Design:** persist taint in an append-only table:

```sql
CREATE TABLE task_reads (
    task_id      TEXT NOT NULL,
    asset_id     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (task_id, asset_id)
);
-- registration (idempotent, concurrency-safe, never removes a restriction)
INSERT INTO task_reads (task_id, asset_id, first_seen_at)
VALUES (?, ?, ?)
ON CONFLICT (task_id, asset_id) DO NOTHING;
```

Rules:

- Registration happens in the **same transaction** as the lineage node write, so a decision can never observe taint that was not recorded, or vice versa.
- Reads are union-only. No application runtime path deletes a `task_reads` row for an active task; retention/expiry is a Phase 10 policy decision, and until it exists nothing expires. The confirmed demo reset (§3.5) is the single exception: an operator action that clears the whole demo dataset, not an authorization path.
- `effective_labels` unions labels from durable `task_reads` for the task. A per-process cache is permitted only if it is bounded and invalidated by the same transaction boundary that writes the row.
- Conservative-mode behaviour is unchanged in direction: more reads can only add restriction.
- The lineage graph mirror (`lineage_nodes` / `lineage_edges`) remains durable as today; taint is separate from graph shape.

**Acceptance:** restart test (read → restart → outbound still tainted) **and** cross-process test (process A reads → process B's outbound is tainted). A test asserting the permissive direction is explicitly forbidden: no code path may reduce the effective label set after a restart.

### 3.3 G-03 — atomic, single-use approval resolution

**Problem:** the gateway checks `status != 'pending'` and later calls `set_status`, which updates with `WHERE approval_id = ?`. Two resolvers can both pass the check.

**Design:** one conditional statement is the claim:

```sql
UPDATE approvals
   SET status = 'allowed_once',
       resolved_at = ?,
       resolved_by = ?          -- reserved; populated in Phase 3
 WHERE approval_id = ?
   AND status = 'pending';
```

- `rowcount == 1` → this process owns the claim and may execute.
- `rowcount == 0` → lost the race, unknown id, or already resolved → **409, nothing executes**. This is a fail-closed path, not an error to retry.
- The claim commits **before** the tool executes. Tool execution must not occur inside the claiming transaction (AD-15: never hold a transaction open across an external call).
- If execution fails after a successful claim, the record must not return to `pending`; the outcome is recorded (Phase 10 owns the unknown-outcome/reconciliation states). Phase 2 owes only: exactly one successful resolution per approval.
- `deny` uses the same conditional pattern (`status = 'denied' WHERE status = 'pending'`), so deny and allow cannot both win.
- **No unconditional status write (F4):** the store exposes no `set_status`-style method. `claim()` accepts only a resolution outcome (`allowed_once`, `denied`, `expanded`); any other value raises before a write, so no caller can bypass the pending-state condition.
- **Pending approvals and restart (F2):** `deny` and `allow_once` need only the frozen reviewed binding and work for any pending record. `expand_task` additionally requires a live review session (§3.1.1), so after a restart it returns 409 without consuming the record; the human can still deny it or allow it once. No expiry duration is invented here.
- **Replay gates against post-review drift (R1/R1b/R3).** `allow_once` was already bound to the reviewed contract (content hash) and the exact tool arguments. It is now also bound to the security state *around* them. Every new check runs BEFORE the claim, returns 409 and leaves the record `pending` for re-review; none of them executes or consumes anything:
  - **R1 — current policy decides hard rules.** The frozen request is re-evaluated against the current policy engine. A current hard BLOCK can never be overridden by an earlier human approval; only the soft APPROVE case is what the human actually granted. (Policy/classification code can change after an approval is created; the approval must not outvote a later hardening rule.)
  - **R1b — tool, destination and connector drift.** The approval stores the reviewed tool identity/action/destination plus a digest of the delivery connector configuration (real-delivery tools only; the webhook URL itself is never stored). A changed registration or connector configuration fails closed, even though the exact arguments are unchanged.
  - **R3 — read content and classification drift.** An approved read stores the asset's SHA-256 content digest and the classification labels captured at review. Changed bytes or labels fail closed, and the verified bytes are the ones that execute (the gateway does not re-read after verifying).
- **Age is visible, not enforced (R2).** The dashboard pending card renders `created_at` and a relative age so a human can judge staleness before clicking Allow Once. **No expiry is enforced anywhere** — retention and any TTL remain a Phase 10 policy decision, and no duration is invented here.

**Acceptance:** N concurrent resolvers → exactly one success and N−1 rejections; exactly one tool execution / one outbox line. SQLite may serialize, but the *guarantee* is accepted only from PostgreSQL (§4.4).

### 3.4 G-16 — one configured backend for gateway and dashboard

**Design:** a single `Persistence` factory constructed from `Config` (§2). Gateway stores, the dashboard, scenarios, and tests obtain their stores from it.

- No module may construct a store with a hardcoded `Path("taskfence.sqlite3")` after Phase 2.
- The dashboard must not open its own SQLite file. It either receives the same factory or reads through the same configured accessor.
- For PostgreSQL, the dashboard uses the same connection settings and read-only queries; it never falls back to a different backend because PostgreSQL is unreachable (AD-05: a projection failure may degrade the dashboard, but must not change which backend is authoritative and must not affect authorization).
- Working directory must not influence which database is used; the SQLite path is resolved once from configuration.

**Acceptance:** a test that starts gateway and dashboard from different working directories and asserts they address the same backend; plus a test that no `DEFAULT_DB_PATH` construction remains in a security path.

### 3.5 Destructive demo reset (operator action, not an application path)

**Problem (F3):** the dashboard's reset control cleared audit/state/approval data, and Phase 2 extended it to the new authoritative tables (`tasks`, `task_contracts`, `task_decisions`, `task_reads`). That is the one code path in the repository that can remove historical decision rows or active-task lineage/taint.

**Rule:**

- The reset is a **destructive demo/test control**, not an application runtime path. Ordinary runtime paths (tool calls, approval resolution, `expand_task`, dashboard reads) never update or delete prior decision records and never delete active-task lineage or taint.
- It requires an explicit confirmation checkbox whose label names what is destroyed (tasks, contracts, decisions, approvals, audit events, lineage, session taint, outbox). The button is not preselected and does nothing while unchecked — it reports that nothing was deleted.
- Only the confirmed branch deletes rows; it also empties the outbox. Tests assert both the refusal and the confirmed clearing, running in a temporary working directory so the repository's tracked sinks are never touched.
- Append-only and monotonic-taint guarantees are described as **application-level** guarantees (API surface + tests). They are not presented as database permissions or triggers; a raw SQL writer with database credentials could still modify rows, which AD-03/Phase 10 hardening must address.

---

## 4. PostgreSQL integration and concurrency acceptance

### 4.1 Tiers and preflight

- Markers: `postgres_integration` (new in Phase 2), `redis_integration` (Phase 7), `live_integration` (existing).
- `TASKFENCE_TEST_DATABASE_URL` selects the test database. It is never the production URL.
- Preflight script exits non-zero **before pytest** when the variable is absent or the database is unreachable. A requested tier never silently skips.
- CI: the PostgreSQL job in `.github/workflows/tests.yml` fails if the service is unavailable.
- Isolation: a dedicated test database, and per-run schema or unique table prefix, so parallel runs cannot share state. Production data is never used.

### 4.2 Test matrix

| Class | Test | Evidence accepted from |
|---|---|---|
| Migration | fresh migrate; second run is a no-op; pre-existing audit/approval/lineage rows survive | PostgreSQL |
| Transactions | contract creation + pointer advance commit atomically; failure leaves no partial contract | PostgreSQL |
| Constraints | duplicate `(task_id, contract_version)`, `(task_id, asset_id)`, duplicate idempotency keys rejected | PostgreSQL |
| Approval claim | concurrent resolution → exactly one success | PostgreSQL |
| Taint | concurrent `task_reads` registration → union preserved, no lost update | PostgreSQL |
| Cross-process | §4.3 | PostgreSQL |
| SQLite parity | local demo and deterministic tier still pass | SQLite |

### 4.3 Real cross-process acceptance test

Two independent interpreters, not threads and not two `TestClient`s in one process:

1. Launch process A (`subprocess` or `multiprocessing` with `spawn`) with `TASKFENCE_DATABASE_URL` pointing at the test database. A creates a task and prints `task_id` plus the contract version.
2. Launch process B, a **fresh interpreter** with the same URL and no shared memory. B submits a tool call for A's `task_id`.
3. Assert B retrieves the persisted contract and evaluates it correctly:
   - an allowed read returns the same outcome A would produce;
   - a destination outside the contract returns `BLOCK`, not a 404 and not an error.
4. Negative control: run the same B script against a database where the task was never created → `404`. This proves B is reading the database rather than inheriting process state.
5. Taint across processes: A reads a restricted asset; B submits an outbound request for the same task. B's decision must reflect the persisted taint (equal to or more restrictive than A's). A test asserting B is less restricted is a failure.
6. Concurrency: N=8 separate processes resolve the same approval simultaneously. Exactly one receives success; the rest receive 409. The outbox contains exactly one line.

Acceptance is the combination: outcome correctness, the negative control, and the concurrency count. Any one alone is insufficient.

### 4.4 SQLite is never evidence of concurrency

- SQLite serializes writes and its `rowcount` behaviour under contention does not demonstrate PostgreSQL row-level semantics.
- Therefore: approval-claim, taint-union, constraint, and transaction-boundary claims are accepted **only** from the PostgreSQL tier.
- SQLite results are accepted for: local demo behaviour, deterministic suite parity, and schema-compatibility smoke checks.
- If PostgreSQL is unavailable, these claims remain unverified and must be reported as such — not converted into a pass.

---

## 5. Phase 2 completion checklist (for the gate, not written yet)

- [x] Precedence rule from §2 implemented; the full matrix in §2.3 passes, including the normalization cases and the conflicting-backend case (`tests/test_config.py`).
- [x] `tasks` / `task_contracts` / `task_decisions` authoritative; `task_state` still projection-only (`tests/test_persistence_decisions.py::test_task_state_is_projection_only`).
- [x] Decision history is append-only: every decision appends a distinct record and no normal write path mutates a prior one (§3.1.1; `tests/test_persistence_decisions.py`, PostgreSQL re-check in `tests/test_postgres_integration.py::test_decisions_are_append_only_on_postgres`).
- [x] `task_reads` append-only; restart and cross-process taint tests pass (`tests/test_cross_process.py`, PostgreSQL union test in `tests/test_postgres_integration.py`).
- [x] Approval claim is a single conditional statement; concurrent test passes on PostgreSQL (`tests/test_postgres_integration.py::test_concurrent_approval_resolution_has_exactly_one_winner`: eight independent interpreters, one 200 / seven 409 / one outbox line).
- [x] Gateway and dashboard resolve one backend (§3.4; `tests/test_backend_unification.py`).
- [x] Migrations idempotent; no data loss (SQLite legacy-upgrade test + PostgreSQL migration test).
- [x] `postgres_integration` marker and preflight (`pytest.ini`, `scripts/postgres_preflight.py`: exit 2 unset / 3 unreachable / 0 reachable).
- [x] Cross-process test passes, including the negative control (`tests/test_cross_process.py`, `tests/test_postgres_integration.py`).
- [x] Deterministic SQLite tier still green; A–G semantics unchanged (`326 passed, 11 deselected`, exit 0 after the F1–F4 follow-up).
- [x] G-01, G-02, G-03, G-16 updated with evidence; each closure cites tests, not design.
- [x] Review follow-up F1–F4: `expand_task` included in the decision-immutability test; post-restart expansion fails closed with the record left pending; the destructive demo reset is confirmation-gated and documented as an operator action (§3.5); the unconditional approval status setter is removed in favour of the conditional claim.
- [x] Approval-lifecycle follow-up R1/R1b/R2/R3: replay re-evaluates current policy and never overrides a current hard rule; tool/destination/connector drift fails closed; approved reads are bound to the reviewed content digest and classification; the dashboard shows approval age (`tests/test_approval_drift.py`, `tests/test_dashboard_smoke.py`). G-28 closed with those tests; **no expiry policy exists yet**, and G-18 stays VERIFY.
- [ ] G-18: `.github/workflows/tests.yml` is written (deterministic job + disposable PostgreSQL job that runs the preflight) but has not been executed here — no GitHub runner in this environment.

---

## 6. Implementation record (2026-10-17)

**Commands and results.**

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | `322 passed, 11 deselected`, exit 0 |
| `python scripts/postgres_preflight.py` (unset) | exit 2 |
| `python scripts/postgres_preflight.py` (unreachable port) | exit 3 |
| `python scripts/postgres_preflight.py` (reachable) | exit 0, PostgreSQL 16.2 |
| `TASKFENCE_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/taskfence_test .venv/bin/python -m pytest -m postgres_integration -q` | `9 passed, 324 deselected`, exit 0 |
| `git diff --check` | exit 0 |

**Review follow-up (F1–F4, 2026-10-18).** F1: the decision-immutability test now covers `expand_task` (earlier rows byte-identical, exactly one new record). F2: post-restart `expand_task` fails closed again — a live review session is process state (this process created the task) and is not persisted, so the record stays `pending` for `deny` / `allow_once`; the stale docstring was corrected and regression tests cover restart, repeat attempts, replay and stale reviewed-version binding. F3: the dashboard reset is confirmation-gated with an explicit destructive label, its two branches are tested, and §3.5 documents it as an operator action whose guarantees are application-level. F4: `ApprovalStore.set_status()` was removed; `claim()` accepts only resolution outcomes and is conditional on `pending`, with a regression test proving no bypass.

| Command (follow-up) | Result |
|---|---|
| `pytest -q` (8 affected modules: decisions, approval persistence/approvals/integrity/e2e, dashboard smoke, no-bypass, gateway integration) | `73 passed`, exit 0 |
| `.venv/bin/python -m pytest -q` | `326 passed, 11 deselected`, exit 0 |
| `scripts/postgres_preflight.py` with `TASKFENCE_TEST_DATABASE_URL` | exit 0, PostgreSQL 16.2 |
| `pytest -m postgres_integration -q` with the same URL | `9 passed, 328 deselected`, exit 0 |
| `git diff --check` | exit 0 |

**Approval-lifecycle follow-up (R1/R1b/R2/R3, 2026-10-18).** A read-only trace of the persisted `allow_once` path (creation → binding → policy → claim → execution after restart) found that one-time use alone did not protect against state drifting around the frozen binding. Fixed with three pre-claim gates plus a display change, each rejection returning 409 with the record left `pending` and no side effect:

- **R1** — replay re-evaluates the frozen request against the current policy engine; a current hard BLOCK can no longer be overridden by the earlier human approval (only the soft APPROVE case is what the human granted).
- **R1b** — the approval stores the reviewed tool identity/action/destination and a digest of the delivery connector configuration (`real_delivery` tools; the webhook URL itself is never stored) and revalidates both before the claim.
- **R3** — an approved read stores the asset's SHA-256 content digest and classification labels captured at review; changed bytes or labels fail closed, and the verified bytes are what execute (the gateway does not re-read after verifying).
- **R2** — the dashboard pending card shows `created_at` and a relative age. No expiry is enforced and no duration was invented.

| Command (R1–R3 follow-up) | Result |
|---|---|
| `pytest -q tests/test_approval_drift.py tests/test_approval_integrity.py tests/test_approval_persistence.py tests/test_approvals.py tests/test_approval_e2e.py tests/test_persistence_decisions.py tests/test_dashboard_smoke.py tests/test_mcp_real_integration.py tests/test_mcp_adapter.py tests/test_gateway_integration.py tests/test_no_bypass.py` | `104 passed, 1 deselected`, exit 0 |
| `.venv/bin/python -m pytest -q` | `341 passed, 11 deselected`, exit 0 |
| `pytest -m postgres_integration -q` with `TASKFENCE_TEST_DATABASE_URL` | `9 passed, 343 deselected`, exit 0 |
| `git diff --check` | exit 0 |

New tests: `tests/test_approval_drift.py` (14 cases: R1 policy hardening, R1b tool/destination/connector drift, R3 content/classification drift, verified-byte execution and the inert-refusal check, plus the unchanged-replay controls), the approval-age dashboard test, and the `set_status` removal test in `tests/test_approvals.py`. G-28 is closed on that evidence; G-18 remains VERIFY.

**Delivered.** `taskfence/persistence.py` (single `Persistence` factory, versioned forward-only schema, explicit SQL, append-only `task_decisions` with a monotonic `seq`, conditional-UPDATE approval claim, append-only `task_reads`, AD-03 reserved columns), the database-precedence matrix and path anchoring in `taskfence/config.py`, gateway/audit/approvals/lineage/dashboard/harness wiring, plus new tests `tests/test_persistence_decisions.py`, `tests/test_cross_process.py`, `tests/test_approval_concurrency.py`, `tests/test_backend_unification.py`, `tests/test_postgres_integration.py` with helpers `tests/proc_child.py` / `tests/child_helpers.py`, preflight `scripts/postgres_preflight.py`, and `.github/workflows/tests.yml`.

**Not delivered / limitations.** The CI workflow has not been executed (no runner here). Redis (`redis_integration`) is Phase 7 and remains absent. The PostgreSQL server used for this run was a local unprivileged bundled instance on `127.0.0.1:55432`; it is test infrastructure, not a project dependency.
