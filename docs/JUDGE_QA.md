# Judge Q&A — TaskFence

**Q: Why not just use a system prompt?**
A system prompt is still an instruction to the agent. TaskFence places
enforcement outside the agent at the tool boundary: the model cannot override
the security decision, because the tool never executes without the gateway's
ALLOW. In scenario B the manipulated agent *tried* to exfiltrate and the sink
stayed empty.

**Q: What if the agent lies about its purpose?**
The agent doesn't define its own purpose. The Task Contract is built from the
user's original task text only (invariant I7 — verified by a spy test that
checks the builder is called exactly once, with the task string, never
document content). Subsequent actions are evaluated against that contract.

**Q: What if the agent legitimately needs more data?**
It gets an APPROVE instead of a silent allow: a human resolves `allow_once`
(executes that one request), `expand_task` (a new contract version with a
parent link — the old contract is never mutated), or `deny`. The agent-side
client never holds the admin token; self-resolution returns 403.

**Q: Is this just DLP?**
No. DLP focuses on what data is sensitive and where it's going. TaskFence adds
a dimension: whether that particular movement is **justified by the task the
user authorized**, tracked through transformations via lineage. We don't claim
DLP can't do purpose-aware controls — TaskFence is complementary to existing
DLP and permission systems, combining task scope, runtime flow, lineage and
enforcement in one agent-facing gateway.

**Q: Why can't the agent bypass TaskFence?**
Sensitive tools aren't exposed to the agent. `taskfence/agent.py` and
`taskfence/client.py` never import `taskfence.tools` (invariant I1, enforced
statically with an AST test and behaviorally: after a BLOCK, the sink file is
byte-identical). The only path is `GatewayClient.call_tool`.

**Q: What happens if TaskFence itself is compromised?**
Out of scope for this prototype, honestly: production deployment would need
hardening of TaskFence itself, strong authentication, process isolation and
tamper-resistant logging. The MVP focuses on the runtime policy/enforcement
layer. The audit log is append-only (no update or delete functions exist),
which is a start, not a finish.

**Q: Does this stop prompt injection?**
No — and we don't claim it does. TaskFence provides a boundary that limits the
*impact* of a manipulated agent: the injection in scenario B succeeded in
changing what the agent attempted, and it still couldn't move the data.
Containment, not prevention.

**Q: What about false positives?**
Disclosed, not hidden. Conservative lineage (everything read this session
taints outbound data) over-blocks legitimate flows in some cases — scenario G
in `reports/results.md` shows one held for human review. The tradeoff is
documented in the code and the report; the resolution is one human approval.

**Q: Why a deterministic policy engine instead of an LLM judge?**
An LLM in the decision path is exactly the thing an attacker can manipulate.
The engine is pure code — no LLM, no network, no file I/O, no clock (invariant
I2, enforced by an AST import test) — with default deny on anything unknown.
The LLM's only security-adjacent job is proposing a contract from the user's
task, and even then deterministic code validates it against a controlled
vocabulary with a keyword fallback when the model is unavailable.

**Q: What's the metric?**
"100% of defined unauthorized flows intercepted in the controlled test suite"
— a controlled MVP test criterion, not a claim of universal security. The
report prints that headline only when it is actually true, and lists false
positives next to it.

**Q: Why MCP?**
MCP (Model Context Protocol) is becoming the standard way AI agents connect
to tools and data sources. Supporting it means TaskFence can sit in front of
tool flows for MCP-capable agents without changing the security model: the
MCP layer (`mcp_gateway/`) is a pure adapter — five tools over stdio — that
forwards every protected call through the same GatewayClient boundary into
the same gateway pipeline. The adapter holds no policy logic (enforced by a
static AST test), returns the gateway's real decision verbatim, and MCP
activity lands in the one existing audit trail with an "MCP agent" origin.
A different protocol reaches TaskFence; none bypasses it.

**Q: Where is the security decision made?**
Always in the TaskFence gateway — never in the model, never in the agent,
never in the MCP adapter. Whether a request arrives from the scripted agent,
the direct-Ollama loop, the MCP agent (`--mcp`) or any external MCP client,
the identical pipeline runs: resolve → classify (incl. outbound rescan) →
lineage taint → policy → audit → execute only on ALLOW. There is one Task
Contract model, one classification, one lineage tracker, one approval store
and one audit trail — the adapter creates no second security model. If no
real gateway decision can be obtained, the adapter fails closed with a
structured `gateway_unavailable` error; it never invents an outcome and
nothing executes.

**Q: What happens on APPROVE?**
The operation is held, not executed. The MCP tool call returns immediately
with the real decision, an approval_id and human instructions — it does not
claim execution, does not wait, and does not retry. The agent-side client
and the MCP adapter never hold the admin token; resolution stays human-only
(dashboard or admin API: `allow_once` executes the exact stored request
once under the reviewed contract binding, `expand_task` issues a new
contract version, `deny` closes it). Until a human resolves, nothing was
sent — including zero HTTP calls when the optional real Slack webhook is
configured.

**Q: Does MCP prevent every possible bypass?**
No. MCP protects the flows that travel through the MCP adapter into
TaskFence. It does not prevent unrestricted direct HTTP calls, shell
commands, or other network egress that a process on the host could make
outside the protected tool path — that is the territory of production
sandbox/network enforcement (next question), which is deliberately out of
scope here. What MCP adds is narrower and real: the standard agent protocol
now has a purpose-bound gateway in front of it, with the same fail-closed,
audited, contract-scoped enforcement as every other agent path.

**Q: What would production sandbox/network enforcement add?**
Enforcement below the tool boundary: OS-level sandboxing of the agent
process, egress allowlists so only the gateway can reach approved
destinations, and network-level controls that mirror the task policy —
closing the direct-HTTP/shell paths the MVP explicitly does not cover.
Those are system-level controls (containers, egress proxies, network
policy), complementary to TaskFence rather than alternatives to it. The
MVP claims only what it verifies: task-scoped runtime enforcement for
tool-mediated data flows.
