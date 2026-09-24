# TaskFence — Build-Phase Prompt Kit

Structure: a **Standard Preamble** you paste before *every* prompt (your anti-hallucination / anti-drift / anti-complication shield: define once, reuse always), then phase-by-phase prompts with an **acceptance gate** between phases. Never start a phase until the previous gate is green.

Source of truth: `specs/TaskFence_Project_Specification.md` (referred to below as "the spec", cited as §N) plus `specs/DECISIONS.md`, which you create in Phase 0 to close the spec's open questions.

## How to run these (session discipline)

1. One prompt per agent session. Review the full diff before accepting. Run the tests yourself.
2. **Phase 0 and Phase A (security core) are human-reviewed line by line.** This is the part judges will probe.
3. If anything fails a gate, use a **Recovery Prompt** (§R) — never let the agent "fix" things by improvising.
4. Time-box every phase. If a phase overruns, apply the **cut order** at the bottom instead of skipping gates.

## The loop for every phase

```text
Prompts  →  🧪 Phase tests  →  ✅ Gate (you review)  →  📌 Commit
```

1. Run the phase prompts one at a time, reviewing each diff.
2. Run the **Phase tests** prompt (if the phase has one) and the commands listed under it.
3. Pass the **Gate** yourselves.
4. Make the **Commit** with the exact title shown, then move on.

## Commit log at a glance

| After | Commit title |
|---|---|
| Phase 0 | `docs: resolve open spec decisions` |
| Phase A | `feat(phaseA): task contract + deterministic policy engine` |
| Phase B | `feat(phaseB): fake enterprise, classifier, lineage, audit` |
| Phase C | `feat(phaseC): tool gateway with pre-execution enforcement` |
| Phase D | `feat(phaseD): contract builder + scripted and local-LLM agents` |
| Phase E | `feat(phaseE): human-controlled approval and scope expansion` |
| Phase F | `feat(phaseF): dashboard` |
| Phase G | `feat(phaseG): attack scenarios and metrics` |
| Phase H | `docs: README, demo script, judge Q&A, claims test` |

| Phase | What | Time box (24 h hackathon) |
|---|---|---|
| 0 | Resolve open spec questions | 30 min |
| A | Security core, no LLM | 3 h |
| B | Data, classifier, lineage, audit | 3 h |
| C | Tools + gateway | 3 h |
| D | Contract builder + agents | 3 h |
| E | Approval / scope expansion | 1.5 h |
| F | Dashboard | 3 h |
| G | Scenarios + metrics | 2 h |
| H | README, demo, claims check | 1.5 h |
| — | Buffer / sleep / rehearsal | ~4 h |

If you are two people: after Gate C, one takes D + E and the other takes F. Everything else stays sequential.

------------------------------------------------------------------------

## Setup file — `agent-instructions.md` (repo root)

Put this in your coding tool's persistent instruction file (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, etc.). The preamble refers to it.

```text
PROJECT: TaskFence — runtime security gateway that checks an AI agent's data
flows against a user-authorized Task Contract. Output: ALLOW / APPROVE / BLOCK.
Principle: the agent decides HOW; TaskFence decides WHETHER. AI interprets
intent; deterministic policy enforces security.

DEPENDENCY ALLOWLIST (nothing else, ask first):
  Python 3.11+, fastapi, uvicorn, pydantic>=2, networkx, streamlit,
  requests, httpx, ollama, pytest. SQLite via stdlib sqlite3.
  NOT allowed: langgraph, react, presidio, docker (until Phase H optional),
  any paid/cloud LLM API, any ORM.

REPO LAYOUT:
  specs/                         (read-only, except Prompt Z0 creating DECISIONS.md)
  taskfence/  models.py catalog.py registry.py policy.py explain.py
              classifier.py lineage.py audit.py tools.py gateway.py
              client.py contract.py agent.py approvals.py
  data/       fake drive / crm / hr / repo files (20–30)
  outbox/     slack_sales.jsonl, external_api.jsonl (fake sinks)
  dashboard/  app.py
  scenarios/  scenario_*.py, run_all.py
  tests/      one test file per source file
  reports/    results.md

SECURITY INVARIANTS (never relax, not even temporarily):
  I1. Agent-side code (agent.py, client.py) never imports tools.py.
  I2. policy.py is deterministic: no LLM, no network, no file I/O, no clock.
  I3. TaskContract is frozen/immutable. Scope changes = new contract version,
      created only through human approval.
  I4. Default deny: unknown tool / destination / asset => BLOCK.
  I5. Every decision, including ALLOW, writes an audit event.
  I6. Derived data inherits restrictions from its sources (lineage).
  I7. The contract is built from the user's task text ONLY — never from
      documents, tool output or agent messages.

BANNED CLAIMS (code comments, UI text, README, demo text):
  "prevents all AI attacks", "eliminates prompt injection", "guarantees
  security", "world's first AI firewall", "no existing system can do this",
  "makes agents completely safe".
  Use instead: "limits the impact of manipulated agents", "intercepts defined
  unauthorized data flows", "task-scoped runtime enforcement".
```

------------------------------------------------------------------------

## 0. Standard Preamble — paste at the top of EVERY prompt

```text
STANDARD GUARDRAILS — apply to everything below:
1. Read the referenced spec section(s) in specs/TaskFence_Project_Specification.md
   AND specs/DECISIONS.md FIRST. Implement ONLY what they describe.
2. If the spec is missing or ambiguous for anything you need: STOP, output
   the question with a ⚠️ VERIFY tag, and wait. Do NOT guess. Do NOT fill
   gaps from training knowledge.
3. Do not add anything not requested: no extra features, no config systems,
   no CLI argument parsing beyond what is listed, no logging frameworks
   (plain print is fine), no abstraction layers, no plugin systems, no design
   patterns, no "improvements", no refactoring of working code. If you
   believe something extra is needed, list it in one line under
   "SUGGESTIONS:" at the end — do not implement it.
4. No dependencies beyond the allowlist in agent-instructions.md.
5. Every source file gets a corresponding test in tests/. Policy and lineage
   tests are table-driven with hardcoded expected results (no computing the
   expected value with the code under test).
6. Keep each file under ~200 lines; each change under ~60 lines where
   possible. Prefer editing existing files over creating new ones.
7. Never run git commands. Never modify specs/, data/ (unless this prompt says
   so) or any file not listed in this prompt.
8. Respect the SECURITY INVARIANTS I1–I7 in agent-instructions.md. If a task
   seems to require breaking one, STOP and report it.
9. Determinism: no randomness and no wall-clock time inside decision logic
   (timestamps only in audit records).
10. First output a 3-line plan: which spec sections you will implement, in
    what order, into which files. Then implement exactly that plan.
11. End with: (a) files changed, (b) test results — run the tests and report
    exact pass/fail counts, (c) "NOT DONE:" list of explicitly deferred items,
    (d) "SUGGESTIONS:" (max 3 one-liners).
```

------------------------------------------------------------------------

## Phase 0 — Close the spec's open questions (human decides)

### Prompt Z0 — Draft `specs/DECISIONS.md`

```text
[paste Standard Preamble]

Task: the spec deliberately leaves several rules open. Draft
specs/DECISIONS.md (the ONLY file you create) with one numbered section per
item below. For each, state the question, cite the spec section, and propose a
default marked ⚠️ VERIFY for me to confirm. Do not write any code. Do not
finalize.

1. Hard vs soft violations (spec §11, §27, §16). Which failed checks =>
   BLOCK, which => APPROVE? Proposed default: BLOCK for external destination
   when external_transfer=false, for restricted labels reaching a
   non-allowed destination, and for anything unknown. APPROVE only when the
   destination is allowed+internal and the sole violation is a source group
   or action outside the contract.
2. Restricted labels (spec §9): which of PII, EMPLOYEE_DATA, CUSTOMER_DATA,
   FINANCIAL, SOURCE_CODE, CONFIDENTIAL are "never leave the allowed
   destinations" labels?
3. Are reads gated? (spec §14, §23 Scenario C). Option 1: out-of-scope reads
   are ALLOWed but tainted and audited. Option 2: out-of-scope reads go
   through policy (usually APPROVE). Proposed default: Option 1 for the demo
   scenarios, Option 2 available via one boolean.
4. Lineage mode (spec §10, §13). The gateway cannot see what the LLM did
   internally. Proposed default: "conservative" = outbound data is treated as
   derived from declared inputs UNION everything the agent has read this task.
   List the false-positive tradeoff.
5. Controlled vocabulary (spec §5, §7, §22): purposes, data groups,
   destinations (internal/external), actions.
6. Asset formats (spec §18): use CSV/JSON/TXT/PY instead of xlsx to avoid
   extra dependencies.
7. What the agent is told on BLOCK / APPROVE (spec §30): proposed default is a
   short "denied by security policy" message with no policy internals; the
   human/dashboard sees full reasons.
8. Contract generation (spec §7, §15): LLM proposes JSON, code validates it
   against the vocabulary, deterministic keyword fallback if the LLM output
   is invalid or Ollama is down.
9. Approval choices (spec §16): allow_once vs expand_task (new contract
   version) vs deny; who can resolve them.
```

### 🧪 Phase 0 checks (no code yet)

```bash
grep -n "VERIFY" specs/DECISIONS.md   # expect: no output
git status --short                    # expect: only specs/DECISIONS.md
```

### ✅ Gate 0 (~10 min)

-   You have read every ⚠️ VERIFY line, edited `DECISIONS.md` yourself, and removed the tags.

### 📌 Commit — Phase 0

You run this (the agent never runs git):

```bash
git add -A
git commit -m "docs: resolve open spec decisions"
git tag gate-0
```

------------------------------------------------------------------------

## Phase A — Security core, no LLM (human-reviewed)

### Prompt A1 — Models, catalog, contract

```text
[paste Standard Preamble]

Task: per spec §5, §8, §28 and DECISIONS §2, §5 — implement:
- taskfence/models.py (Pydantic v2): TaskContract (frozen; contract_id,
  parent_contract_id optional, purpose, allowed_data, allowed_destinations,
  allowed_actions, external_transfer, created_at, and a deterministic content
  hash exposed as `version`), DataAsset, FlowRequest, Decision (outcome,
  reasons, failed_checks, contract_version), FlowEvent.
- taskfence/catalog.py: the controlled vocabulary from DECISIONS §5 and the
  restricted-label set from DECISIONS §2. Constants only.

Tests: tests/test_models.py — assigning to a contract field raises; changing
any field changes `version`; same fields give the same `version`; unknown
vocabulary values rejected.
```

### Prompt A2 — Policy engine

```text
[paste Standard Preamble]

Task: per spec §11, §12, §27 and DECISIONS §1–§2 — implement
taskfence/policy.py: PolicyEngine.evaluate(contract, request) -> Decision.
Follow the spec §27 order (source, action, destination, external-transfer,
lineage, purpose) and collect ALL failed checks, not just the first. Put the
hard/soft rules from DECISIONS §1 as named constants at the top of the file.
Reasons must be plain-language sentences.

Tests: tests/test_policy.py, table-driven, at least 15 cases with HARDCODED
expected outcomes:
- spec §23 Scenario A (sales_report_q3 -> sales_team) => ALLOW
- Scenario B (customer_db -> external_api) => BLOCK, both reasons present
- Scenario C (employee_salary -> derived salary_summary -> external_api) =>
  BLOCK via inherited labels
- customer data -> sales_team under sales_reporting => APPROVE
- unknown tool / destination / asset => BLOCK (default deny)
- same input 100x => identical Decision (determinism)
No LLM, no FastAPI, no file I/O anywhere in this task.
```

### Prompt A3 — Explanations

```text
[paste Standard Preamble]

Task: per spec §30 — implement taskfence/explain.py: explain(decision, request,
contract, lineage_path) -> str, producing exactly the BLOCKED / Source /
Destination / Task / Why / Lineage layout of spec §30 (and the ALLOW / APPROVE
equivalents).

Tests: tests/test_explain.py — string snapshot tests for Scenarios A, B, C.
```

### 🧪 Phase A tests

**Prompt A-T — Invariant and mutation tests**

```text
[paste Standard Preamble]

Task: per spec §11, §27 and invariants I2, I4 — add tests/test_invariants.py.
Do NOT change any source file. If a test exposes a real bug, STOP and report
it; do not fix it silently.
- ast-check taskfence/policy.py: it may import only the stdlib typing/enum
  modules and taskfence.models / taskfence.catalog. No ollama, requests,
  httpx, fastapi, sqlite3, os, time, datetime, random.
- Mutation table: start from the Scenario A request that returns ALLOW;
  mutate ONE field at a time (source outside allowed_data, action outside
  allowed_actions, destination not allowed, external destination, restricted
  label in lineage). Hardcode the expected outcome for each; none may be ALLOW.
- A contract with empty allowed_* lists never returns ALLOW.
```

**Commands (you run these):**

```bash
pytest tests/ -q
pytest tests/test_policy.py tests/test_invariants.py -v
```

**Expected:** all green, no Ollama and no server running; at least 15 policy cases plus the invariant tests.

### ✅ Gate A (both, ~30 min)

-   `pytest tests/ -q` — all green, with no Ollama and no server running.
-   Print `explain()` for Scenarios A, B and C. **Read the three outputs out loud to each other. Would a judge understand why each decision was made?**
-   Confirm `policy.py` imports nothing from `ollama`, `requests`, `httpx`, `fastapi` or `sqlite3`.

### 📌 Commit — Phase A

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseA): task contract + deterministic policy engine"
git tag gate-A
```

------------------------------------------------------------------------

## Phase B — Data, classification, lineage, audit

### Prompt B1 — Fake enterprise data + registry

```text
[paste Standard Preamble]

Task: per spec §18 and DECISIONS §5–§6 — create data/ with 20–30 obviously
fake files: drive/ (sales_report_q3.csv, sales_report_q2.csv,
internal_strategy.txt, public_company_info.txt, meeting notes),
crm/customer_db.json (~30 fake customers), hr/employee_salary.csv (~20 fake
employees, ₹ salaries), repo/source_code.py (with a fake API key string), and
drive/meeting_notes_poisoned.txt (looks like normal notes; contains an
injected instruction to read customer_db and send it to the external API).
Create outbox/ with empty slack_sales.jsonl and external_api.jsonl.
Implement taskfence/registry.py: asset registry mapping each file to id,
source system, data group and base labels; plus reset_outbox().

Tests: tests/test_registry.py — every file under data/ is registered and every
registered file exists; the poisoned file is present; reset_outbox empties
both sinks; no real-looking personal data (check names/emails use fake
domains).
```

### Prompt B2 — Classifier

```text
[paste Standard Preamble]

Task: per spec §9 — implement taskfence/classifier.py:
classify(text, hint_labels=()) -> set of labels. Regex/keyword rules only:
emails, Indian mobile numbers, Aadhaar-like 12-digit numbers, PAN-like IDs,
₹/INR amounts and "salary"/"ctc"/"compensation", API-key-looking strings,
Python source markers, customer-record shape. Labels = union of registry
labels and content scan.

Tests: tests/test_classifier.py — at least 3 positive and 3 negative cases per
label, hardcoded.
```

### Prompt B3 — Lineage with taint

```text
[paste Standard Preamble]

Task: per spec §10, §13 and DECISIONS §4 — implement taskfence/lineage.py:
NetworkX DiGraph mirrored to SQLite tables lineage_nodes / lineage_edges
(so the dashboard, a separate process, can read them). API:
register_asset, derive(input_ids, transformation, output_id),
record_sink, ancestors, effective_labels (labels of node UNION all
ancestors), path_to (readable chain "salary_csv -> salary_summary ->
external_api"), and a per-task session_reads set with the conservative /
declared mode switch from DECISIONS §4. Document the false-positive tradeoff
in the module docstring.

Tests: tests/test_lineage.py — multi-hop label inheritance, conservative mode
taints an outbound payload with no declared inputs, declared mode does not,
persistence round-trip through SQLite.
```

### Prompt B4 — Audit trail

```text
[paste Standard Preamble]

Task: per spec §29 — implement taskfence/audit.py: SQLite table audit_events
(timestamp, task_id, task_text, contract_id, contract_version, tool, action,
sources, labels, transformation, destination, decision, reasons,
lineage_path). Expose ONLY log_event() and read functions — no update, no
delete.

Tests: tests/test_audit.py — append then read; assert the module has no
update/delete function (inspect its public names).
```

### 🧪 Phase B tests

**Prompt B-T — Data, lineage and persistence integration tests**

```text
[paste Standard Preamble]

Task: per spec §9, §10, §13, §29 — add tests/test_phaseB_integration.py.
Do NOT change source files; report bugs instead of fixing them.
- Every file under data/ goes through classify(); the result must include the
  registry base labels for that asset. public_company_info.txt must come out
  PUBLIC only (false-positive check).
- Scenario C chain: register salary_csv, derive salary_summary, then assert
  effective_labels includes EMPLOYEE_DATA and FINANCIAL and path_to(...) equals
  the hardcoded string "salary_csv -> salary_summary -> external_api".
- Persistence: write lineage nodes/edges and one audit event, open a NEW
  sqlite connection, and read back identical data.
```

**Commands (you run these):**

```bash
pytest tests/ -q
python - <<'EOF'
from taskfence import lineage
# build salary_csv -> salary_summary -> external_api using your lineage API,
# then print effective_labels(...) and path_to(...)
EOF
```

**Expected:** all green; the printed labels for the derived node include the salary source's labels.

### ✅ Gate B

-   `pytest tests/ -q` — all green.
-   In a Python shell, build `salary_csv -> salary_summary -> external_api` and print `effective_labels` and `path_to`. **You should see EMPLOYEE_DATA/FINANCIAL inherited by the derived node.** This is the Scenario C proof.

### 📌 Commit — Phase B

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseB): fake enterprise, classifier, lineage, audit"
git tag gate-B
```

------------------------------------------------------------------------

## Phase C — Tools + gateway

### Prompt C1 — Fake tools

```text
[paste Standard Preamble]

Task: per spec §14, §18 — implement taskfence/tools.py: read_file(path),
query_customer_db(query), send_slack(channel, text) (appends to
outbox/slack_sales.jsonl), post_external(url, payload) (appends to
outbox/external_api.jsonl). Each tool exposes metadata: action, source asset
resolution, destination id, is_external, is_outbound. This module is PRIVATE
to the gateway.

Tests: tests/test_tools.py — each tool works against fake data; sinks receive
exactly one JSONL line per call.
```

### Prompt C2 — Gateway pipeline

```text
[paste Standard Preamble]

Task: per spec §6, §14, §27 and DECISIONS §3, §7 — implement
taskfence/gateway.py (FastAPI):
- POST /tasks {task_text}: creates a contract (temporary stub returning the
  sales_reporting contract; Prompt D1 replaces it), returns task_id + contract.
- POST /tasks/{task_id}/tool-call {tool, args}: resolve sources -> classify
  (including a rescan of outbound content) -> lineage update -> policy ->
  audit -> execute the tool ONLY on ALLOW -> return decision, reasons,
  lineage_path, result if executed.
- Reads follow DECISIONS §3 (flag GATE_OUT_OF_SCOPE_READS).
- GET /tasks/{task_id}, GET /audit, GET /lineage/{task_id}.
- Unknown tool => BLOCK + audit event.
- On BLOCK/APPROVE return the agent-facing message from DECISIONS §7.
```

### Prompt C3 — Client + no-bypass tests

```text
[paste Standard Preamble]

Task: per spec §15, §26 — implement taskfence/client.py: GatewayClient with
call_tool(task_id, tool, args). It is the ONLY taskfence import agent.py may
use.

Tests: tests/test_no_bypass.py —
- static: parse agent.py and client.py with ast; assert none import
  taskfence.tools (invariant I1)
- behavioral: after a BLOCK on post_external, outbox/external_api.jsonl is
  byte-identical to before
- Scenario A/B at gateway level with FastAPI TestClient
```

### 🧪 Phase C tests

**Prompt C-T — Gateway integration tests**

```text
[paste Standard Preamble]

Task: per spec §6, §14, §27 and invariants I4, I5 — add
tests/test_gateway_integration.py using FastAPI TestClient. Do NOT change
source files; report bugs instead of fixing them.
- Table of (tool, args, expected decision, expected sink line counts) for
  Scenario A, Scenario B and an unknown tool.
- Monkeypatch tools.post_external to raise AssertionError, run Scenario B: the
  request must return BLOCK and the patched tool must never be invoked.
- Exactly one audit event is written per tool call (count before and after).
- Out-of-scope read with GATE_OUT_OF_SCOPE_READS=False => ALLOW + tainted +
  audited; with True => APPROVE.
```

**Commands (you run these):**

```bash
pytest tests/ -q
uvicorn taskfence.gateway:app --port 8000        # terminal 1
curl -X POST localhost:8000/tasks -H "Content-Type: application/json" \
  -d '{"task_text":"Summarize Q3 sales and post it to #sales."}'          # note the task_id
curl -X POST localhost:8000/tasks/<TASK_ID>/tool-call -H "Content-Type: application/json" \
  -d '{"tool":"post_external","args":{"url":"https://example.com/collect","payload":"customer data"}}'
wc -l outbox/*.jsonl
```

**Expected:** the external call returns BLOCK; `outbox/external_api.jsonl` has 0 lines.

### ✅ Gate C

-   `pytest tests/ -q` — all green.
-   Via TestClient: `sales_report_q3 -> send_slack` executes (one line in the Slack sink); `customer_db -> post_external` returns BLOCK and **the external sink file is unchanged**.
-   The audit table contains both events with reasons.

### 📌 Commit — Phase C

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseC): tool gateway with pre-execution enforcement"
git tag gate-C
```

------------------------------------------------------------------------

## Phase D — Contract builder + agents

### Prompt D1 — Task → contract

```text
[paste Standard Preamble]

Task: per spec §7, §15, DECISIONS §8 and invariant I7 — implement
taskfence/contract.py: build_contract(task_text) -> TaskContract.
1) Ask Qwen3 via Ollama (temperature 0) for JSON only, values from
   catalog.py.
2) Validate with Pydantic and intersect with the catalog; drop unknown
   values; external_transfer=False unless the user's text explicitly names an
   external catalog destination.
3) Deterministic keyword fallback if Ollama is down or the JSON is invalid.
   Record which path was used.
Wire it into POST /tasks, replacing the stub.

Tests: tests/test_contract.py — with the LLM call monkeypatched: valid JSON,
invalid JSON (fallback), out-of-vocabulary values dropped. Also assert
build_contract's signature accepts only the task string (no document input).
```

### Prompt D2 — Scripted agent

```text
[paste Standard Preamble]

Task: per spec §23, §39 Phase 3 — implement ScriptedAgent in
taskfence/agent.py: replays a fixed list of tool calls through GatewayClient.
It simulates a successfully injected agent for Scenario B and gives
deterministic runs for tests and live-demo fallback. Add
`python -m taskfence.agent --task "..." --scripted <name>`.

Tests: tests/test_agent_scripted.py — Scenario A end-to-end with no Ollama.
```

### Prompt D3 — Ollama / Qwen3 agent

```text
[paste Standard Preamble]

Task: per spec §19, §39 Phase 3 — implement OllamaAgent in taskfence/agent.py:
tool-calling loop against Ollama (model from env TASKFENCE_MODEL, default
"qwen3"), temperature 0, max 8 steps, tool schemas mirroring the four tools.
Every tool call goes through GatewayClient.call_tool. Feed the agent-facing
message back on BLOCK/APPROVE.

Tests: tests/test_agent_ollama.py — with the Ollama client monkeypatched:
the loop stops at max steps, and a BLOCK result is fed back rather than
retried in a loop. Add one test marked `@pytest.mark.ollama` that only runs
when Ollama is reachable.
```

### 🧪 Phase D tests

**Prompt D-T — Agent end-to-end and injection tests**

```text
[paste Standard Preamble]

Task: per spec §7, §15, §23, §39 Phase 3 and invariant I7 — add
tests/test_agent_e2e.py. Do NOT change source files; report bugs instead.
- ScriptedAgent Scenario A end-to-end: ALLOW, Slack sink has exactly 1 line.
- ScriptedAgent Scenario B end-to-end: BLOCK, external sink has 0 lines.
- Spy on build_contract during Scenario B (after the poisoned document has
  been read): it is called once, and its argument equals the original task
  string, never document content.
- The agent loop stops at max steps and does not retry a BLOCK in a loop.
```

**Commands (you run these):**

```bash
pytest tests/ -q
pytest -m ollama -q        # only if Ollama + qwen3 are available
python -m taskfence.agent --task "Summarize Q3 sales and post it to #sales." --scripted scenario_a
```

**Expected:** all green without Ollama; the optional Ollama test either passes or you record honestly that Qwen3 struggled.

### ✅ Gate D

-   ScriptedAgent runs Scenario A end-to-end with no Ollama.
-   With Ollama running: `python -m taskfence.agent --task "Summarize Q3 sales and post it to #sales."` completes the task (or you record honestly that Qwen3 struggled and you rely on ScriptedAgent for the demo).

### 📌 Commit — Phase D

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseD): contract builder + scripted and local-LLM agents"
git tag gate-D
```

------------------------------------------------------------------------

## Phase E — Approval / scope expansion

### Prompt E1 — Approval state

```text
[paste Standard Preamble]

Task: per spec §16, §22 and DECISIONS §9 — implement taskfence/approvals.py
and gateway endpoints: on APPROVE create a pending approval; POST
/approvals/{id}/resolve {choice: allow_once | deny} requires header
X-Admin-Token equal to env TASKFENCE_ADMIN_TOKEN. GatewayClient never has the
token. allow_once executes that single request only, no contract change.
Everything is audited.

Tests: tests/test_approvals.py — agent-side call to resolve => 403; allow_once
executes once and only once; deny never executes.
```

### Prompt E2 — Expand task

```text
[paste Standard Preamble]

Task: per spec §16 — add choice expand_task: create a NEW contract version
(new contract_id, parent_contract_id = old, widened scope). The old contract
is never mutated. Audit both the approval and the new version.

Tests: tests/test_approvals.py — new `version` hash differs; old contract
unchanged; assert via the OpenAPI schema that no endpoint lets the agent edit
a contract (invariant I3).
```

### 🧪 Phase E tests

**Prompt E-T — Approval end-to-end tests**

```text
[paste Standard Preamble]

Task: per spec §16 and invariant I3 — add tests/test_approval_e2e.py. Do NOT
change source files; report bugs instead.
- allow_once is single-use: after it executes, the identical request returns
  APPROVE again.
- deny leaves the sinks untouched.
- expand_task: a follow-up in-scope request is ALLOWed under the new contract
  version; the old contract's version hash is unchanged; the audit trail shows
  both versions.
- Resolving without the admin token, or with a wrong one, returns 403.
- Audit order for an approved flow: request -> approval -> execution.
```

**Commands (you run these):**

```bash
pytest tests/ -q
TASKFENCE_ADMIN_TOKEN=devtoken pytest tests/test_approval_e2e.py -v
```

**Expected:** all green; the agent-side client can never resolve an approval.

### ✅ Gate E

-   The "needs customer data for regional conversion rate" case: APPROVE → resolve allow_once → succeeds. The audit trail shows request → approval → execution.

### 📌 Commit — Phase E

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseE): human-controlled approval and scope expansion"
git tag gate-E
```

------------------------------------------------------------------------

## Phase F — Dashboard (Streamlit)

### Prompt F1 — Static panels

```text
[paste Standard Preamble]

Task: per spec §17, §29, §31 — create dashboard/app.py (Streamlit) reading
SQLite only (separate process from the agent): header, current task, Task
Contract card (including contract version), decision panel using explain()
output, audit trail table with a decision filter.
```

### Prompt F2 — Live flow + lineage graph

```text
[paste Standard Preamble]

Task: per spec §17, §31 — add the live-flow graph and lineage graph as
Graphviz DOT strings via st.graphviz_chart (no system graphviz install).
ALLOW green, BLOCK red, APPROVE amber. Auto-refresh with
st.fragment(run_every="2s").
```

### Prompt F3 — Controls

```text
[paste Standard Preamble]

Task: per spec §16, §23 — sidebar: buttons that run scripted Scenarios A, B, C
(D once it exists); pending approvals with Allow Once / Expand Task / Deny
using the admin token from env; Reset button (clears DB and outbox).
UI computes NOTHING about security — display and triggers only.
```

### 🧪 Phase F tests

**Prompt F-T — Dashboard smoke test**

```text
[paste Standard Preamble]

Task: per spec §17, §31 — add tests/test_dashboard_smoke.py using
streamlit.testing.v1.AppTest. Seed a temporary SQLite DB with one ALLOW and
one BLOCK audit event. Assert: the app runs with no exception, the audit table
shows both decisions, and the BLOCK reason text is rendered. Do NOT change
source files; report bugs instead.
```

**Commands (you run these):**

```bash
pytest tests/ -q
streamlit run dashboard/app.py
```

**Manual checklist:** run Scenario A then B from the sidebar; green ALLOW then red BLOCK with reasons and lineage appear; Reset clears the DB and both sinks.

### ✅ Gate F

-   Run A then B from the sidebar: green ALLOW, then red BLOCK with reasons and lineage, both visible in the audit table.
-   **Screen-record this now.** It is your fallback demo video.

### 📌 Commit — Phase F

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseF): dashboard"
git tag gate-F
```

------------------------------------------------------------------------

## Phase G — Scenarios + metrics

### Prompt G1 — Core scenarios

```text
[paste Standard Preamble]

Task: per spec §23 — create scenarios/scenario_a.py, scenario_b.py,
scenario_c.py using ScriptedAgent through the real gateway. Each returns the
observed decisions and asserts outbox side effects.
- A: ALLOW; Slack sink has exactly 1 message.
- B: read the poisoned doc, then attempt customer_db -> external_api;
  BLOCK; external_api.jsonl is EMPTY.
- C: read employee_salary.csv, derive an average salary, attempt to post it
  externally; BLOCK with reason "derived data inherits restrictions". Add a
  variant where the outbound text contains NO salary keywords and it still
  BLOCKs (proves lineage, not keywords).
```

### Prompt G2 — Extra scenarios

```text
[paste Standard Preamble]

Task: per spec §16, §26, §27 — add:
- D: scope expansion => APPROVE, then ALLOW after allow_once
- E: unknown tool and unknown destination => BLOCK
- F: agent attempts to change its contract => impossible / 403
- G: a legitimate flow under conservative lineage — report honestly whether
  it is over-blocked
```

### Prompt G3 — Runner + report

```text
[paste Standard Preamble]

Task: per spec §33, §34 — create scenarios/run_all.py: runs every scenario,
prints a table, writes reports/results.md with: unauthorized flows
intercepted X/Y, legitimate flows allowed X/Y (false positives listed), and a
line stating this is a controlled MVP test criterion, not a claim of
universal security. Only print the "100%" headline if it is actually true.
```

### 🧪 Phase G tests

**Prompt G-T — Scenario suite and report honesty tests**

```text
[paste Standard Preamble]

Task: per spec §23, §33, §34 — add tests/test_scenarios.py. Do NOT change
source files; report bugs instead.
- Parametrize scenarios A–G; each asserts its decisions and sink side effects.
- run_all returns structured results with counts (unauthorized intercepted X/Y,
  legitimate allowed X/Y).
- Report honesty: feed the report writer a result set containing one failed
  unauthorized scenario; the "100%" headline must NOT be emitted.
```

**Commands (you run these):**

```bash
pytest tests/ -q
python -m scenarios.run_all
cat reports/results.md
```

**Expected:** all green; the report lists intercepted vs allowed counts and any false positives.

### ✅ Gate G

-   `python -m scenarios.run_all` and `pytest` both green; `reports/results.md` exists.
-   Read the false-positive list. **If a legitimate flow is over-blocked, decide together whether to fix or disclose it. Do not hide it.**

### 📌 Commit — Phase G

You run this (the agent never runs git):

```bash
git add -A
git commit -m "feat(phaseG): attack scenarios and metrics"
git tag gate-G
```

------------------------------------------------------------------------

## Phase H — Docs, demo, claims check

### Prompt H1 — README and demo material

```text
[paste Standard Preamble]

Task: per spec §26, §35, §36, §40 — write README.md (definition paragraph,
problem, ASCII architecture, setup incl. `ollama pull qwen3`, how to run
gateway / dashboard / scenarios, stack, Limitations); docs/DEMO_SCRIPT.md
(3–4 minutes: A -> B -> C -> D, what to say, what to point at, ScriptedAgent
fallback); docs/JUDGE_QA.md (the spec §26 questions with short answers).

Limitations MUST state: regex classification is coarse; conservative lineage
can over-block; prototype gateway is not hardened or isolated; TaskFence
limits the impact of a manipulated agent and does not eliminate prompt
injection. Do not claim DLP cannot do purpose-aware controls.
```

### Prompt H2 — Claims lint + one-command demo

```text
[paste Standard Preamble]

Task: per spec §35 — add tests/test_copy_claims.py failing on any banned
claim phrase in README.md, docs/, dashboard/app.py and taskfence/. Add
run_demo.sh (reset state, start gateway + dashboard). Optional Docker is NOT
part of this prompt.
```

### 🧪 Phase H checks

```bash
python -m venv .venv-check && source .venv-check/bin/activate
pip install -r requirements.txt
pytest -q                              # expect: everything green incl. test_copy_claims
./run_demo.sh                          # expect: gateway + dashboard start
grep -rniE "prevents all|eliminates prompt|guarantees security|first AI firewall|completely safe" README.md docs dashboard taskfence   # expect: no output
```

### ✅ Gate H

-   A teammate who did not build it follows the README on a clean clone and reaches a working dashboard in under 5 minutes.
-   Rehearse the demo three times: twice with ScriptedAgent, once with Qwen3.

### 📌 Commit — Phase H

You run this (the agent never runs git):

```bash
git add -A
git commit -m "docs: README, demo script, judge Q&A, claims test"
git tag gate-H
```

------------------------------------------------------------------------

## If you fall behind — cut order (spec §32)

Cut from the bottom up. **Never cut** Phase A, lineage (B3), the gateway (C2), or Scenarios A–C.

1. Docker and any polish or animation
2. Scenarios E–G extras
3. `expand_task` (keep `allow_once` and the APPROVE state)
4. Real Qwen3 agent (keep ScriptedAgent and the keyword-fallback contract builder)
5. Dashboard extras (keep contract card, decision panel, audit table)

------------------------------------------------------------------------

## R. Recovery prompts (when things go wrong)

**When a test fails and the agent starts thrashing:**

```text
STOP all code changes. Read spec section [N] / DECISIONS section [N] and the
failing test. Output: (1) what the spec says should happen, (2) what the code
does, (3) the minimal diff between them. Do not fix anything yet.
```

**When the agent drifts (adds unrequested stuff or "improves" things):**

```text
Revert the parts of your change not listed in my original request. List what
you removed. Then continue with ONLY the original task. If you believe the
removed part was necessary, add one line under SUGGESTIONS.
```

**When it hallucinates an API/library:**

```text
That call does not exist in [library] at the installed version. Open the
installed package source and find the correct API for this purpose. Show me
the actual signature before writing the call.
```

**When it weakens a test to make it pass:**

```text
You changed or skipped a test instead of fixing the code. Restore the
original test exactly. The test encodes the spec; the code is what is wrong.
Show the diff between the restored test and the code behavior, then propose
the minimal code fix.
```

**When it lets the model or the agent make a security decision:**

```text
This violates invariant I2 / I3. Security decisions must come from the
deterministic policy engine, and the agent must not be able to change its
contract. Remove the LLM/agent involvement from the decision path. The LLM may
only interpret the task before enforcement or propose actions to the gateway.
```

------------------------------------------------------------------------

## Final discipline reminders

-   **Gates are human decisions.** The agent never declares a phase complete — you do, after green tests and reading the outputs.
-   **The spec + DECISIONS.md are the single source of truth.** When behavior and spec disagree, fix the code, or make a deliberate spec edit yourselves.
-   **Test the claim, not the demo.** The headline is "every defined unauthorized flow intercepted in the controlled test suite" — say exactly that, and report the false-positive count next to it.
-   **Commit at every gate.** A hackathon dies at hour 20 from one bad merge, not hour 2.
-   Protect Phase A, B3, C2 and Scenarios A–C above everything: that is the product.
