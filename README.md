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

- **Deterministic policy engine** — no LLM, no network, no file I/O, no clock
  (invariant I2); default deny on anything unknown (I4).
- **Data classification** — regex/rule-based labels (PII, EMPLOYEE_DATA,
  CUSTOMER_DATA, FINANCIAL, SOURCE_CODE, CONFIDENTIAL, INTERNAL, PUBLIC).
- **Lineage with taint** — derived data inherits its sources' restricted
  labels; conservative mode unions the session's reads (DECISIONS §4).
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
pytest -m ollama -q    # optional real-model test, only runs when reachable
```

### Scenarios and metrics

```bash
.venv/bin/python -m scenarios.run_all    # writes reports/results.md
.venv/bin/pytest -q                      # full test suite
```

## Demo scenarios

| Scenario | Story | Result |
|---|---|---|
| A | Legitimate: summarize Q3 sales → #sales | ALLOW, message delivered |
| B | Poisoned meeting notes instruct exfiltration | read allowed-but-tainted; external post BLOCKed; sink untouched |
| C | Derive average salary → post externally | BLOCKed via inherited EMPLOYEE_DATA/FINANCIAL — even with **no salary keywords** in the payload (C2) |
| D | Agent needs customer data for conversion rates | APPROVE → human allow_once → executes exactly once; expand_task creates a new contract version |
| E | Unknown tool / unknown asset | default-deny BLOCK, audited |
| F | Agent tries to change its own contract | impossible — no such endpoint, no client surface |
| G | Legitimate flow after an out-of-scope read | held for human review (conservative taint) — **disclosed false positive**, see report |

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
             agent · approvals
data/        fake drive / crm / hr / repo files (all fictional, .invalid domains)
outbox/      slack_sales.jsonl · external_api.jsonl (fake sinks)
dashboard/   Streamlit app (reads SQLite only)
scenarios/   scenario_a..g + run_all.py
tests/       one test file per source file + integration/e2e/no-bypass/claims
reports/     results.md (generated by scenarios.run_all)
specs/       project specification + DECISIONS.md (source of truth)
```
