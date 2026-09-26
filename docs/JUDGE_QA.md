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
