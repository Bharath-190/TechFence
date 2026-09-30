# TaskFence — Purpose-Bound Security for AI Agents

**Track 3 — Cybersecurity & Defense · ASYNC'26**

TaskFence is a runtime security gateway for AI agents. It sits between an
autonomous agent and the tools/data sources it can use. When a user starts a
task, TaskFence creates a **Task Contract** — purpose, permitted data, actions
and destinations — derived only from the user's task text. The agent then
operates autonomously, but every sensitive tool/data flow is intercepted by
TaskFence, which evaluates source, destination, action, data classification and
full **lineage** against the contract. It allows legitimate flows, requests
human approval for plausible scope expansion, and blocks unauthorized flows
**before the tool executes**. Enforcement happens outside the AI model, so the
agent cannot override its own security policy.

> The agent decides **HOW** to accomplish the task.
> TaskFence decides **WHETHER** the requested data movement is permitted.

## The problem

Traditional access control asks: *"Can the agent access this resource?"*
An agent with legitimate access to a customer database, internal files and
external APIs can still be manipulated (indirect prompt injection, poisoned
documents, compromised tools) into chaining those permissions into an
unauthorized flow. TaskFence adds the missing question:

> *"Is this specific data movement justified by the task the user authorized?"*

## Architecture

In one line: **User Task → Task Contract → AI Agent → TaskFence → ALLOW /
APPROVE / BLOCK → Tool**

```text
User Task
    ↓
Task Contract  (frozen; built from task text ONLY — invariant I7)
    ↓
AI Agent  (ScriptedAgent fallback · Ollama/Qwen3 tool loop)
    ↓  GatewayClient — the only agent-side path
TaskFence Gateway  (FastAPI)
    resolve → classify → lineage taint → policy → audit
    ↓
ALLOW → tool executes      APPROVE → human resolves            BLOCK → tool never runs
                           (allow_once / expand_task / deny,
                            X-Admin-Token; agent gets 403)
```

### Key security properties

- **Enforcement outside the agent** — every decision happens in the
  gateway, never in the model; the agent cannot override its own policy.
- **Deterministic policy** — no LLM, no network, no file I/O, no clock
  (invariant I2); default deny on anything unknown (I4).
- **Immutable contracts** — frozen at creation and hash-versioned; changing
  a contract is impossible (no mutating endpoint — Scenario F).
- **Data classification** — regex/rule-based labels (PII, EMPLOYEE_DATA,
  CUSTOMER_DATA, FINANCIAL, SOURCE_CODE, CONFIDENTIAL, INTERNAL, PUBLIC).
- **Lineage and derived-data inheritance** — derived data inherits its
  sources' restricted labels; conservative mode unions the session's reads
  (DECISIONS §4).
- **Append-only audit** — every decision, including ALLOW and BLOCK, is
  recorded with reasons and the lineage chain (I5); no update or delete.
- **Human approvals** — the agent-side client never holds the admin token.
- **Dashboard** — Streamlit, reads SQLite only; renders the contract card,
  live flow, decision explanations, lineage graph and audit trail. The UI
  computes nothing about security.

## Quickstart

Requires Python 3.11+. No paid cloud LLM API is used; everything runs locally.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_demo.sh          # resets state, starts gateway :8000 + dashboard :8501
```

Then open http://localhost:8501 and press **Scenario A** in the sidebar —
or drive the gateway directly:

```bash
# terminal 1
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/uvicorn taskfence.gateway:app --port 8000
# terminal 2
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/streamlit run dashboard/app.py
# terminal 3 — scripted demo flows (no Ollama needed)
.venv/bin/python -m taskfence.agent --task "Summarize Q3 sales and post it to #sales." --scripted scenario_a
.venv/bin/python -m taskfence.agent --task "Summarize Q3 sales and post it to #sales." --scripted scenario_b
```

### Optional: the real local model

The contract builder and agent work without Ollama (deterministic keyword
fallback + ScriptedAgent). To use Qwen3:

```bash
ollama pull qwen3
ollama serve
pytest -m ollama -q    # optional real-model test (skips if unavailable)
```

The marked test skips with an explicit reason when Ollama is unreachable or
when the configured model (`TASKFENCE_MODEL`, default `qwen3`) is not pulled
— it never fails the suite and never silently passes. With both available it
runs a real Qwen3 agent end-to-end through the gateway and asserts the
gateway-enforcement invariants on that live path: the gateway's decision
(not the model) controls tool execution, the agent reaches tools only via
GatewayClient, and every decision is retrievable via `GET /audit`. The
default `pytest -q` run stays fully green without Ollama (the test is
deselected by default).

### Optional: real Slack delivery (flag-gated)

By default Slack messages are delivered only to the fake local sink
(`outbox/slack_sales.jsonl`) — no HTTP call is made. Allowed flows can
deliver to ONE real Slack incoming webhook when the environment variable
`TASKFENCE_REAL_SLACK_WEBHOOK` is set in the **gateway** process (setup
and guarantees: `mcp_gateway/README.md`). BLOCK and APPROVE send zero
HTTP calls; ALLOW sends exactly one; the human-approved `allow_once`
replay executes exactly once. The webhook URL is a secret: never
hardcoded, committed or logged, and never written to audit payloads or
sink records. Normal tests never touch the real network; the single live
check is deselected by default (`pytest -m live_integration`).

## MCP Gateway (optional MCP extension)

TaskFence also speaks MCP (Model Context Protocol), so MCP-capable AI
agents reach the **same** gateway. The MCP layer is an **adapter, not a
second security engine**: it holds no policy logic, never decides
ALLOW/APPROVE/BLOCK and never executes tools directly — every protected
call is evaluated by the existing gateway pipeline and the real outcome
is returned verbatim (design decisions: `specs/MCP_DECISIONS.md`;
implementation: `mcp_gateway/`, pinned `mcp==2.2.0`).

```text
Real AI agent  (Ollama/Qwen3 loop: python -m taskfence.agent --mcp)
      ↓  MCP over stdio (official mcp SDK)
TaskFence MCP gateway  (mcp_gateway.server — adapter ONLY)
      ↓  GatewayClient / HTTP — the same boundary every agent uses
TaskFence gateway  (the ONLY security authority)
      ↓  real Decision + audit + gateway-side execution
ALLOW / APPROVE / BLOCK
```

### Local setup

```bash
# terminal 1 — the security authority
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/uvicorn taskfence.gateway:app --port 8000
# terminal 2 — the MCP adapter (stdio; used by any MCP client or the agent)
.venv/bin/python -m mcp_gateway.server
# terminal 3 — the real-model MCP agent (needs Ollama running)
.venv/bin/python -m taskfence.agent --task "Summarize Q3 sales and post it to #sales." --mcp
```

The agent discovers tools **dynamically** over MCP (`tools/list`); the
five tools are `read_file`, `search_drive`, `query_crm`, `send_slack` and
`post_external`. `search_drive` returns metadata candidates only — search
is not permission; each read is individually evaluated. Calls without a
task context fail closed; APPROVE returns an approval_id with human
instructions and never claims execution; an unreachable gateway returns a
structured `gateway_unavailable` error instead of an invented decision.
Details: `mcp_gateway/README.md`.

### MCP scenarios and dashboard evidence

```bash
.venv/bin/python -m scenarios.mcp_runner    # writes reports/mcp_evidence.json
```

Runs MCP A (ALLOW → exactly one Slack sink line), MCP B (BLOCK → external
sink byte-unchanged) and MCP C/C2 (BLOCK through lineage, keyword-free
payload) over the real MCP stdio boundary against a real gateway, with an
isolated state directory (repo outbox untouched). The Streamlit dashboard
renders the stored evidence verbatim in an additive panel (origin
"MCP agent") — no existing dashboard calculation changed.

### MCP success criteria actually verified

By the deterministic test suite (`tests/test_mcp_adapter.py`,
`tests/test_agent_mcp.py`, `tests/test_mcp_scenarios.py`,
`tests/test_mcp_real_integration.py`):

- MCP server starts locally; exactly the five intended tools discover.
- Protected MCP requests pass through the existing gateway; MCP code never
  imports `taskfence.policy/lineage/tools/audit` (static AST + behavioral
  tests) and no second policy/classification/lineage engine exists.
- A = ALLOW (one sink line); B = BLOCK (sink byte-identical); C = BLOCK
  through lineage with a zero-keyword payload.
- APPROVE returns approval_id and does not execute; BLOCK is never retried.
- MCP activity lands in the ONE existing `audit_events` trail.
- Slack flag: ALLOW → exactly one HTTP POST; BLOCK/APPROVE → zero; human
  `allow_once` → exactly one; URL never logged, committed or audited.

> **Ollama integration has not been exercised in this environment.** All
> green results come from the deterministic fallback and scripted flows.

### Scenarios and metrics

```bash
.venv/bin/python -m scenarios.run_all    # writes reports/results.md
.venv/bin/pytest -q                      # full test suite
```

## Security metrics (live)

Every figure below is generated, never hand-written — regenerate with the
commands above and the README stays honest:

- **Scenario coverage:** 9 scenarios reproduced (A, B, C, C2, D, D2, E, F, G)
  — the scenario modules wired into `scenarios/run_all.py`.
- **Unauthorized flows intercepted:** **5/5** — from
  `reports/results.md`, regenerated by `python -m scenarios.run_all`.
- **Legitimate flows allowed:** **2/3** — same source; the 1 held flow
  (Scenario G) is the disclosed conservative-taint false positive.
- **Approval-gated flows:** counted live from the audit log (APPROVE
  decisions) on the dashboard's SECURITY METRICS panel, alongside exact
  one-time approvals executed (`approval_resolve` ALLOW events).
- **Auditability:** the dashboard shows an audit-coverage check over its
  stored session (every event carries a decision + reasons; every stored
  task has audit events) — deliberately **not** a bare "100%": the
  honest, verifiable form of that claim, per the no-invented-metrics rule.

## Demo scenarios

| Scenario | Story | Result |
|---|---|---|
| A | Legitimate: summarize Q3 sales → #sales | ALLOW, message delivered |
| B | Poisoned meeting notes instruct exfiltration | read allowed-but-tainted; external post BLOCKed; sink untouched |
| C | Derive average salary → post externally | BLOCKed via inherited EMPLOYEE_DATA/FINANCIAL |
| C2 | Same exfiltration, keyword-free payload | BLOCKed via inherited labels alone — rewording dodges nothing |
| D | Agent needs customer data for conversion rates | APPROVE → human allow_once → executes exactly once |
| D2 | Contract scope formally expanded | expand_task creates a new contract version; the old version stays intact (control) |
| E | Unknown tool / unknown asset | default-deny BLOCK, audited |
| F | Agent tries to change its own contract | impossible — no such endpoint, no client surface |
| G | Legitimate flow after an out-of-scope read | held for human review (conservative taint) — **disclosed false positive**, see report |

## Test results

Two runs on this machine, both reported exactly as observed:

```text
# Deterministic verification (Ollama unreachable via env override):
TASKFENCE_OLLAMA_URL=http://127.0.0.1:1 .venv/bin/pytest -q
226 passed, 1 deselected, 1 warning in 9.17s

# Plain run (Ollama installed and reachable, default qwen3 model):
pytest -q
2 failed, 224 passed, 1 deselected, 1 warning in 2050.54s (34m10s)
```

The two plain-run failures are environmental, not code defects. With Ollama
reachable, every `POST /tasks` sends the contract-drafting prompt to the
real qwen3 model, whose response takes ~20 s; two tests that drive a real
gateway over HTTP (`test_agent_cli.py::test_scripted_cli_scenario_b_prints_gateway_denial_message`,
`test_outbox_isolation.py::…[tests/test_scenarios.py]`) then exceed their
own 10 s / 300 s timeouts. Both pass in 2.17 s when the LLM contract path is
disabled — proving the failures are latency-only, and that the deterministic
fallback contract is what all tests pin. The one deselected test is the
optional live Ollama smoke test (`pytest.ini` deselects `ollama` by default);
the warning is the existing FastAPI/Starlette `TestClient` deprecation
warning. No test was skipped, weakened, or rewritten to produce these
numbers.

After the MCP extension (adapter, `--mcp` agent, MCP scenarios, optional
real Slack flag), the same deterministic command reports — exactly as
observed:

```text
TASKFENCE_OLLAMA_URL=http://127.0.0.1:1 .venv/bin/pytest -q
261 passed, 2 deselected in 46.56s
```

The two deselected tests are the optional live Ollama smoke test and the
optional live Slack webhook delivery (`live_integration`), both excluded
from the deterministic suite by `pytest.ini`.

## Stack

Python · FastAPI · Pydantic v2 · SQLite (stdlib) · NetworkX · rule-based
classification · Streamlit · Ollama/Qwen3 (optional) · pytest.

## Limitations

This is a hackathon MVP; the following are known and intentional:

- **Regex classification is coarse** — it demonstrates deterministic labeling,
  not production DLP. It will miss formats it has no rule for.
- **Conservative lineage can over-block** — outbound data is treated as derived
  from everything the agent read this session (DECISIONS §4), so a legitimate
  message can be held for review (scenario G in `reports/results.md`,
  disclosed rather than hidden).
- **The prototype gateway is not hardened or isolated** — a production
  deployment would need hardening of TaskFence itself, strong authentication,
  isolation, and tamper-resistant logging.
- **TaskFence limits the impact of a manipulated agent** by intercepting
  defined unauthorized data flows. It does **not** prevent all AI attacks and
  does **not** eliminate prompt injection — it contains what a manipulated
  agent can do.
- TaskFence adds task-purpose and lineage context to runtime decisions; it is
  complementary to existing DLP and permission systems, not a replacement.
- The headline metric in `reports/results.md` is a **controlled MVP test
  criterion** — every *defined* unauthorized flow intercepted in the test
  suite — not a claim of universal security.

## Repo layout

```text
taskfence/   models · catalog · policy · explain · classifier · lineage
             audit · registry · tools · gateway · client · contract
             agent · approvals · mcp_client
mcp_gateway/ MCP stdio adapter — five tools; adapter, not a security engine
data/        fake drive / crm / hr / repo files (all fictional, .invalid domains)
outbox/      slack_sales.jsonl · external_api.jsonl (fake sinks)
dashboard/   Streamlit app (reads SQLite only)
scenarios/   scenario_a..g, mcp_scenario_a..c · run_all.py · mcp_runner.py
tests/       one test file per source file + integration/e2e/no-bypass/claims
reports/     results.md + mcp_evidence.json (generated)
specs/       project specification + DECISIONS.md + MCP_DECISIONS.md
```
