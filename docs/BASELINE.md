# TaskFence — Phase 0 Baseline

**Phase:** 0 (read-only baseline review)
**Branch:** `freebuff/982aaf89d38627b2d9e6864c`
**HEAD:** `bf6418f` — "Import cloud project files"
**Date recorded:** 2026-10-17
**Scope:** no application source, test, dependency, or schema change. The only files written in this phase are documentation.

---

## 1. Git state at baseline

```
$ git rev-parse --abbrev-ref HEAD
freebuff/982aaf89d38627b2d9e6864c

$ git status --short
?? docs/ARCHITECTURE_DECISIONS.md
?? docs/GAP_REGISTER.md
?? docs/THREAT_MODEL.md
?? docs/implementation/
```

The only working-tree entries are the four Phase -1 artifacts, all untracked. No tracked file is modified. `taskfence.sqlite3` and `outbox/*.jsonl` are generated at runtime and are covered by `.gitignore`; they do not appear in status.

Phase -1 artifacts present:

| Path | Size |
|---|---|
| `docs/THREAT_MODEL.md` | 19,151 B |
| `docs/GAP_REGISTER.md` | 12,743 B |
| `docs/ARCHITECTURE_DECISIONS.md` | 27,334 B |
| `docs/implementation/STANDARD_AGENT_PREAMBLE.md` | 6,896 B |

---

## 2. Preamble verification against pack v4 §6

**Result: identical, line for line (103 lines each), no drift.**

Method: extracted the fenced `text` block from `docs/implementation/STANDARD_AGENT_PREAMBLE.md` and diffed it against §6 of the v4 prompt pack.

```
pack_lines=103 canonical_lines=103
PREAMBLE: identical to pack v4 §6 (line-for-line)
```

**Comparison basis and limitation:** the pack file at `/home/bharath/Downloads/Telegram Desktop/TaskFence_Free_Stack_Implementation_Prompts_v4.md` is not present in this VM (it is on the reviewer's machine, and the cloud workspace has no access to local paths). The comparison therefore used the v4 §6 content attached to this conversation. If the on-disk pack has been edited since it was attached, this check would not detect that; the human reviewer should confirm the attachment and the file are the same version.

No reconciliation was performed, because no drift was found.

---

## 3. Environment

Created from the repository's existing instructions only (`requirements.txt`); no dependency added, removed, or edited.

```
python3 -m venv .venv
.venv/bin/python -m pip install --quiet -r requirements.txt     # exit 0
```

- Python 3.12.3, pip 24.0
- No `.env` file exists; `.env.example` does not exist (G-22)

Installed versions relevant to later phases (full `pip freeze` was captured at install time):

```
fastapi==0.143.3      pydantic==2.14.1     httpx==0.28.1
streamlit==1.66.0     networkx==3.7        pytest==9.1.1
mcp==2.2.0            mcp-types==2.2.0     cryptography==50.0.2
PyJWT==2.15.1         opentelemetry-api==1.45.1
```

Two observations, neither acted on:

- `PyJWT==2.15.1`, `cryptography==50.0.2`, and `opentelemetry-api==1.45.1` are already present as transitive dependencies (via `mcp`). Phase 3 and Phase 9 must confirm what they actually add and must not claim a new pin where a transitive one already exists.
- `requirements.txt` has no version pins, so this environment is reproducible in shape but not bit-for-bit. Recorded as G-29.

---

## 4. Deterministic suite — exact result

Command, exactly as documented for the phase:

```bash
TASKFENCE_OLLAMA_URL=http://127.0.0.1:1 .venv/bin/pytest -m "not postgres_integration and not redis_integration and not live_integration" -q
```

```
pytest_exit=0
272 passed, 1 skipped, 1 deselected in 81.42s (0:01:21)
```

`git diff --check`:

```
diff_check_exit=0
```

Breakdown of the non-passing items:

| Item | Count | What it is | Why |
|---|---|---|---|
| skipped | 1 | `tests/test_agent_ollama.py::test_real_ollama_loop_completes_scenario_a` | Observed reason at `tests/test_agent_ollama.py:147`: `Ollama not reachable at http://127.0.0.1:1`. Skipped with an explicit reason, not silently. |
| deselected | 1 | `tests/test_mcp_real_integration.py::test_live_real_slack_webhook_delivery` | Marked `live_integration` and excluded by the `-m` expression; confirmed by `--collect-only -m live_integration`. |

Total collected under this command: **274** (272 passed + 1 skipped + 1 deselected).

**Test-selection finding.** The command's `-m` expression replaces the `-m` in `pytest.ini`'s `addopts` (`-m "not ollama and not live_integration"`). Consequently the `ollama` marker is no longer honoured by this command, and the Ollama test is collected and skipped at runtime rather than deselected. With a reachable Ollama it would execute, so this "deterministic" tier is environment-dependent. Recorded as evidence on G-17; not changed in this phase.

**This is a fresh run.** No historical count was reused.

---

## 5. Integration tiers — unavailable, reported separately

None of these were exercised. A skip or an absent marker is **not** evidence that the integration works.

| Tier | Configuration | Client library | Status |
|---|---|---|---|
| PostgreSQL | `TASKFENCE_DATABASE_URL`, `TASKFENCE_TEST_DATABASE_URL` both UNSET | `psycopg` not importable | Not available |
| Redis / Upstash | `UPSTASH_REDIS_URL`, `UPSTASH_REDIS_TOKEN` UNSET | `redis` not importable (REST path not implemented) | Not available |
| Gemini (live) | `GEMINI_API_KEY` UNSET | HTTP only, no adapter yet | Not available |
| Auth0 | `TASKFENCE_AUTH0_ISSUER`, `TASKFENCE_AUTH0_AUDIENCE` UNSET | no JWT validation code yet | Not available |
| Slack (live) | `TASKFENCE_REAL_SLACK_WEBHOOK` UNSET | `httpx` present | Not available |
| Ollama / Qwen3 | no server reachable at the probe URL | — | Not available |

No `postgres_integration` or `redis_integration` marker exists in the repository yet; the marker names in the command are forward-looking and currently match zero tests.

---

## 6. Latency baseline (preliminary, Phase 0)

The prescribed local deterministic path is available, so a baseline was recorded. It is a **preliminary single-process measurement**, not the Phase 9 before/after comparison and not a production claim.

**Method.** In-process `fastapi.testclient.TestClient` against `taskfence.gateway:app`, warm SQLite in the repository root, one task (`sales_reporting` contract), 20 warm-up iterations then 200 measured iterations per path. Each measurement covers the full handler: policy evaluation, audit insert, lineage write, `task_state` projection write. No outbound network call is involved.

| Path | p50 | p95 | p99 | mean | min | max |
|---|---|---|---|---|---|---|
| ALLOW (`read_file` sales report) | 3.433 ms | 3.969 ms | 4.845 ms | 3.504 ms | 2.899 ms | 14.419 ms |
| BLOCK (unregistered asset) | 3.365 ms | 3.794 ms | 4.270 ms | 3.433 ms | 2.965 ms | 10.249 ms |

Sanity check: the two paths returned `ALLOW` and `BLOCK` respectively, so the comparison is between genuinely different decision paths.

Caveats that must accompany these numbers: single process, single machine, single run, no confidence interval, includes TestClient overhead, and the SQLite tables grow during the run. No project target has been set; `AD-24` remains `VERIFY` until a human sets one and Phase 9 re-measures against it.

---

## 7. Architecture summary

```
user task text ──> POST /tasks ──> contract builder (Ollama proposal + deterministic
                                   validator + keyword fallback) ──> TaskContract (frozen,
                                   content-hash version) stored in CONTRACTS (process-local)

agent ──> GatewayClient ──> POST /tasks/{id}/tool-call
                               │
                               ├─ read/query: registry resolve ─> classify ─> policy
                               └─ outbound:   declared ∪ session-taint labels ─> policy
                               │
                               ├─ ALLOW   → tools.execute ─> outbox / optional real Slack POST
                               ├─ APPROVE → ApprovalStore.create (FIX1 binding)
                               └─ BLOCK   → nothing executed
                               │
                               └─ audit (SQLite) + lineage (SQLite) + task_state (display projection)

approvals ──> admin token ──> allow_once (exact replay) | expand_task (new version) | deny
```

- Security authority: `taskfence/policy.py`, deterministic, no network/file/clock.
- Boundaries: model and MCP adapter propose; only the gateway executes.
- MCP: `mcp_gateway/server.py` is adapter-only and forwards through `GatewayClient`.

Full evidence: `docs/THREAT_MODEL.md` §1.

---

## 8. Persistence surfaces

| Surface | Backing | Authority |
|---|---|---|
| `CONTRACTS`, `TASK_TEXT`, `LAST_DECISION` | process memory (`gateway.py:55-57`) | **authoritative for task resolution today** |
| `session_reads` | process memory (`lineage.py:41`) | **authoritative for conservative taint today** |
| `audit_events` | SQLite (`audit.py`) | append-only record |
| `task_state` | SQLite (`audit.py` `TaskStateStore`) | display projection only |
| `approvals` | SQLite (`approvals.py`) | durable approval records |
| `lineage_nodes` / `lineage_edges` | SQLite (`lineage.py`) | durable graph mirror |

Gateway stores resolve `taskfence.sqlite3` relative to CWD; the dashboard resolves `TASKFENCE_DB` (`dashboard/app.py:23,545`). `scenarios/harness.py:94-101` sidesteps both by constructing each store with an explicit temp path.

---

## 9. Model / agent entry points

| Entry point | File | Notes |
|---|---|---|
| Contract construction | `taskfence/contract.py:build_contract_with_path` | Ollama proposal, deterministic validation, keyword fallback |
| Scripted agent | `taskfence/agent.py:ScriptedAgent` | fixed flows for scenarios A–C |
| Local model agent | `taskfence/agent.py:OllamaAgent` | temperature 0, `MAX_STEPS = 8`, stops on BLOCK/APPROVE |
| MCP agent | `taskfence/agent.py:McpAgent` | same loop through the MCP bridge |
| Agent-side client | `taskfence/client.py:GatewayClient` | only taskfence module agent code may import |
| MCP adapter | `mcp_gateway/server.py` | five tools, no decision logic |

No cloud model provider exists yet.

---

## 10. Files likely to change in Phases 1–13 (inspection-based forecast)

| Phase | Likely files |
|---|---|
| 1 | new `taskfence/config.py`; `.env.example`; `gateway.py`, `contract.py`, `agent.py`, `client.py`, `tools.py`, `mcp_gateway/server.py`, `dashboard/app.py` (env reads); `pytest.ini` |
| 2 | new `taskfence/persistence/` (PostgreSQL + SQLite), migrations; `gateway.py`, `audit.py`, `approvals.py`, `lineage.py`, `scenarios/harness.py`; `requirements.txt`; `pytest.ini`; `.github/workflows/tests.yml` |
| 3 | new `taskfence/identity.py`; `gateway.py`; `config.py`; `requirements.txt`; tests |
| 4 | new `taskfence/authorization.py`; `gateway.py`; `policy.py` (compose, not rewrite); tests |
| 5 | `catalog.py`, `registry.py`, `tools.py`, `gateway.py`; tests |
| 6 | new `taskfence/credentials.py`; `tools.py`, `gateway.py`, `mcp_gateway/server.py`; docs |
| 7 | new `taskfence/budgets.py`; `gateway.py`, `tools.py` (typed usage envelope), `agent.py` (turn cap); `.github/workflows/tests.yml` |
| 8 | new `taskfence/providers/`; `agent.py`, `contract.py` (provider selection), `config.py`; tests |
| 9 | new `taskfence/telemetry.py`; `gateway.py`, `agent.py`, `providers/`; `requirements.txt` |
| 10 | `gateway.py`, `audit.py`, `approvals.py`, `lineage.py`; new reconciliation surface; health/readiness routes; tests |
| 11 | new connector module(s); `credentials.py`, `registry.py`, `gateway.py`; tests |
| 12 | `README.md`, `docs/JUDGE_QA.md` → `docs/FAQ.md`, `docs/DEMO_SCRIPT.md` → `docs/PRODUCT_WALKTHROUGH.md`, `dashboard/app.py` (copy only), `tests/test_copy_claims.py` |
| 13 | documentation only, plus any test required to fix a verified regression |

Forecast only. Each phase inspects before changing.

---

## 11. Gap re-verification against source and tests

All four remain **OPEN**; no status change was justified.

| Gap | Verified evidence today | Correction? |
|---|---|---|
| G-01 | `gateway.py:55-57` dicts; `tool_call` reads `CONTRACTS` and returns 404 on miss (`:289-292`); `STATE` is written at `:230` and never read by a decision path; `scenarios/harness.py:94-101` rebinds the dicts per run | None. Refinement already recorded in Phase -1: display persistence exists, decision state does not. |
| G-02 | `session_reads` declared at `lineage.py:41`, written at `:72`, read at `:141`; no serialization; dashboard only displays the value from the state snapshot (`dashboard/app.py:1051`) | None. |
| G-03 | `set_status` UPDATE has no state predicate (`approvals.py:149-155`); `WHERE status = 'pending'` appears only in listing queries (`:141,:145`); no threaded/concurrent approval test exists (the threads in `tests/` are HTTP servers for MCP and dashboard tests) | None. |
| G-16 | gateway stores default to `Path("taskfence.sqlite3")` (`audit.py:12,45,113`, `approvals.py:28,52`, `lineage.py:23,33`); dashboard uses `TASKFENCE_DB` (`app.py:23,545`); `scenarios/mcp_runner.py:147` sets `TASKFENCE_DB` only for the dashboard subprocess | None; the harness's explicit-path construction is additional evidence that the production path has no single resolution. |

---

## 12. Blockers, ambiguities, and open decisions

1. **`AD-05` (availability posture) is not yet accepted by a human reviewer.** Phase -1's gate requires this. It is not marked complete and Phase 1 must not begin on the assumption that it was accepted.
2. **The prompt pack file is not readable from this VM.** All pack comparisons in this phase used the attached content. If the two differ, the file wins and this baseline must be corrected.
3. **Unpinned dependencies** (G-29): the environment is reproducible in shape, not by version.
4. **Canonical command changes marker selection** (evidence on G-17): the `-m` on the command line replaces `pytest.ini`'s `addopts` expression, so `ollama` is no longer deselected.
5. **No integration tier is configured** (G-17, G-18): PostgreSQL, Redis, and all live providers are unverified, not passing.

---

## 13. Reproduce this baseline

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

git rev-parse --abbrev-ref HEAD
git status --short

TASKFENCE_OLLAMA_URL=http://127.0.0.1:1 .venv/bin/pytest \
  -m "not postgres_integration and not redis_integration and not live_integration" -q
git diff --check
```

Expected: `272 passed, 1 skipped, 1 deselected`, exit 0; `git diff --check` exit 0. Counts are valid for commit `bf6418f` and the installed versions above only.
