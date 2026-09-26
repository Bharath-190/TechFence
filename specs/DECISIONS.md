# TaskFence — Decisions Log

**Status: DRAFT — pending Gate 0 (human review).**

This file closes the open questions left by `specs/TaskFence_Project_Specification.md`
(cited below as §N). Every section carries a proposed default marked with a
`⚠️ VERIFY` line. **The human owner must read each default, edit anything they
disagree with, and delete every `⚠️ VERIFY` line** — Gate 0 is only passed when
this file contains no VERIFY markers (kit Phase 0 check: `grep -n "VERIFY"` must
return nothing).

Once confirmed, this file is the second source of truth (with the spec). Phases
A–H cite it as "DECISIONS §N". Section numbering below is permanent; later
phases must not renumber.

---

## 1. Hard vs soft violations — which checks BLOCK, which APPROVE

**Question:** The spec gives three outcomes (ALLOW / APPROVE / BLOCK, §11) and a
ten-step check order (§27), but never says which failed checks are hard (BLOCK)
versus soft (escalate to APPROVE, §16).

**Spec:** §11 (decision outcomes), §16 (scope expansion), §27 (decision logic),
invariant I4 (default deny).

**Proposed default:**

- **BLOCK (hard)** when any of:
  - destination is **external** while `external_transfer=false`;
  - a **restricted label** (see §2) would reach a destination not allowed for
    that label — including via lineage-inherited labels (§13);
  - the **tool, asset, or destination is unknown** to the catalog (I4: default
    deny — unknown things are never soft);
  - the action itself is an out-of-scope **outbound** action
    (e.g. `post_external` not in `allowed_actions`).
- **APPROVE (soft)** only when **all** of: destination is allowed **and**
  internal; no restricted label is involved; and the sole violations are a
  **source data group** or **action** outside the contract (i.e. plausibly
  legitimate, human can judge).
- Everything else BLOCKs. When in doubt, the harder outcome wins.

Rationale: keeps judges' trust — escalation exists for "legitimately needs more
data" (§26 Q3), not for exfiltration-shaped flows.

⚠️ VERIFY — confirm this hard/soft split (edit here, then delete this line).

---

## 2. Restricted labels — the "never leave allowed destinations" set

**Question:** Of the classifier labels in §9, which are *restricted*, meaning
they can never reach a destination outside the destinations allowed for their
data group, even if some contract clause seems to permit it?

**Spec:** §9 (classification labels), §13 (derived data), invariant I6.

**Proposed default:** restricted = **PII, EMPLOYEE_DATA, CUSTOMER_DATA,
FINANCIAL, SOURCE_CODE**.

- `CONFIDENTIAL` is **contextual**, not restricted: it may flow to destinations
  allowed for its data group by the contract (e.g. sales reports → sales_slack).
- Restricted labels propagate through lineage to all derived data (§13, I6).
- `PUBLIC` is unrestricted.

Effect: Scenario C blocks even if the LLM's proposed contract omitted the
salary group, because the inherited EMPLOYEE_DATA/FINANCIAL labels trip the
hard rule in DECISIONS §1.

⚠️ VERIFY — confirm the restricted set and CONFIDENTIAL's contextual status
(edit here, then delete this line).

---

## 3. Are reads gated?

**Question:** When the agent reads a source outside its contract (e.g. reads
`employee_salary.csv` during a sales task), does the read itself go through the
policy engine, or is it allowed-but-tainted?

**Spec:** §14 (tool boundary), §23 Scenario C (the salary read must happen for
the demo to work), §26 Q3.

**Proposed default:** **Option 1 — out-of-scope reads are ALLOWed, but the read
taints the session (lineage) and is audited.** Rationale: Scenarios B and C need
the agent to actually obtain the sensitive data so the *outbound* attempt is
observable and blocked — that is the demo's money shot. A flag
`GATE_OUT_OF_SCOPE_READS = False` (module constant in `gateway.py`) switches to
**Option 2 — out-of-scope reads go through policy and usually APPROVE**, in case
the team prefers demonstrating read gating instead.

Either way the read is audited (I5) and the taint is recorded (I6).

**CONFIRMED (human decision, FIX-D5 phase, 2026-09-27):** Option 1 stands as
the default — out-of-scope reads are ALLOWed but taint the session and are
audited, with the `GATE_OUT_OF_SCOPE_READS = False` boolean escape hatch.

---

## 4. Lineage mode — conservative vs declared inputs

**Question:** The gateway cannot see what the LLM did internally between tool
calls, so what counts as the origin of outbound data?

**Spec:** §10 (lineage), §13 (derived-data protection).

**Proposed default:** **Conservative mode** — any outbound payload is evaluated
as if derived from `declared_inputs UNION everything the agent has read during
this task session`. Declared mode (union of explicitly declared inputs only) is
available via the same mode switch for comparison.

**Known tradeoff (must be disclosed, never hidden — kit Gate G):** conservative
mode can **over-block legitimate flows**: e.g. a genuine task that reads sales
data *and* public info, then also read the salary file earlier in the session,
would see its outbound message tainted by EMPLOYEE_DATA. For the hackathon demo
this is acceptable and honest; `reports/results.md` must list any such false
positive rather than suppress it.**CONFIRMED (human decision, FIX-D5 phase, 2026-09-27):** Conservative mode
stands — **Session-taint scope: KEEP CONSERVATIVE** (Option A of the
post-build fix kit). Every asset read during the task may contribute to
outbound lineage; Scenario G's APPROVE false positive is accepted and must
remain disclosed in `reports/results.md` (legitimate flows allowed: 2/3).
There is no requirement to improve that figure, and taint code must not be
changed merely to make the metric look better.

FIX1 interaction (recorded explicitly before the demo): because session
reads act as implicit outbound source context, an outbound request that
declares NO source assets may still be APPROVEable (soft
`source_outside_contract`) from session-taint groups alone. FIX1 makes
`expand_task` fail closed for such requests (no declared source group to
expand), so the only human resolution is repeated `allow_once`. This is
accepted friction, not a defect.

---

## 5. Controlled vocabulary

**Question:** The spec requires a controlled vocabulary for contracts (§5, §7,
§22) but never fixes the actual terms. Contracts may only contain values from
these lists; anything else is dropped at contract build (DECISIONS §8) or
BLOCKed at evaluation (I4).

**Spec:** §5, §7, §22, §28; invariant I4.

**Proposed default:**

- **Purposes:** `sales_reporting`, `customer_support`, `hr_analytics`,
  `code_review`, `market_research`
- **Data groups (assets):** `sales_reports`, `customer_db`, `employee_salary`,
  `internal_strategy`, `public_info`, `source_code`, `meeting_notes`
- **Destinations — internal:** `sales_slack` (#sales), `hr_portal`,
  `internal_wiki`, `repo`
- **Destinations — external:** `external_api`, `personal_email`,
  `public_paste` (demo stand-ins for "anywhere outside")
- **Actions:** `read`, `summarize`, `query`, `derive`, `send_message`,
  `post_external`

The four fake tools map onto: `read_file`→`read`, `query_customer_db`→`query`,
`send_slack`→`send_message`, `post_external`→`post_external`.

⚠️ VERIFY — confirm or amend the term lists (edit here, then delete this line).

---

## 6. Asset formats

**Question:** §18 suggests `sales_report.xlsx`; shipping xlsx support needs an
extra dependency outside the kit's allowlist.

**Spec:** §18 (fake environment), kit dependency allowlist.

**Proposed default:** **CSV / JSON / TXT / PY only** — no xlsx, no openpyxl.
Fake dataset stays at 20–30 obviously-fake files (fake domains, fake phone
numbers) per kit Prompt B1.

⚠️ VERIFY — confirm the format substitution (edit here, then delete this line).

---

## 7. What the agent is told on BLOCK / APPROVE

**Question:** §30 wants explainability for humans; but full policy internals in
the agent's context would let a manipulated agent probe the policy (and judges
may ask why the attacker learns the rules).

**Spec:** §30 (explainable UX), §26 ("why can't the agent bypass TaskFence?").

**Proposed default:** on **BLOCK or APPROVE**, the agent receives a short fixed
message — e.g. `"This request was denied by security policy. A human has been
notified."` — with **no reasons, no contract fields, no lineage details**. Full
reasons, lineage and failed checks go to the **human side only**: dashboard,
`explain()` output, and audit trail. On ALLOW the agent gets the tool result
plus a one-line confirmation.

⚠️ VERIFY — confirm the minimal-disclosure agent message (edit here, then
delete this line).

---

## 8. Contract generation — LLM proposes, code disposes

**Question:** How is the natural-language task turned into a TaskContract
without letting the model become a security decision-maker (invariant I7, §15)?

**Spec:** §7 (task parser), §15 (enforcement outside the agent), §26 Q2 ("what
if the agent lies about its purpose?"), invariant I7.

**Proposed default:**

1. Qwen3 via Ollama (temperature 0) is asked for **JSON only**, with values
   drawn from DECISIONS §5 vocabulary.
2. Code **validates and intersects**: unknown vocabulary values are dropped;
   `external_transfer` is `False` unless the user's task text explicitly names
   an external catalog destination (never the reverse).
3. If Ollama is down or the JSON is invalid → **deterministic keyword
   fallback** builder (no network, pure rules).
4. Which path was used (`llm` or `fallback`) is recorded on the contract and in
   the audit trail.

`build_contract(task_text)` accepts **only the task string** — never documents,
tool output or agent messages (I7). The LLM proposes intent; deterministic code
owns the contract.

⚠️ VERIFY — confirm propose/validate/fallback pipeline and the
external_transfer default (edit here, then delete this line).

---

## 9. Approval choices and who resolves them

**Question:** §16 lists Allow Once / Expand Task / Deny but not the mechanics:
state, authorization, and effect on the contract.

**Spec:** §16 (approval / scope expansion), §22 (MVP scope), invariant I3.

**Proposed default:**

- On APPROVE, the gateway creates a **pending approval** holding the original
  FlowRequest.
- Resolution is a POST with header `X-Admin-Token` matching env
  `TASKFENCE_ADMIN_TOKEN`. **GatewayClient never holds the token** — the agent
  side cannot resolve approvals (gets 403). The human resolves via dashboard
  buttons or direct call with the token.
- `allow_once` — executes exactly that one stored request; no contract change;
  a repeat of the same request APPROVEs again.
- `expand_task` — creates a **new contract version** (new `contract_id`,
  `parent_contract_id` = old, widened scope). The old contract is **never
  mutated** (I3); both versions appear in the audit trail.
- `deny` — nothing executes; sinks untouched.
- The approval event, the resolution, and any resulting execution are each
  audited (I5).

⚠️ VERIFY — confirm the approval mechanics and admin-token separation (edit
here, then delete this line).

---

## 10. Approval record retention and privacy (FIX1)

**Question:** FIX1 stores the EXACT original tool args (`tool_args`, including
the full outbound payload) in the approval record so a human-approved request
replays byte-for-byte. Sensitive data may therefore live in the approvals
table — what are the storage rules?

**Spec:** §16 (approval), §29 (audit), FIX1-D (approval-data retention).

**Decision (MVP):** approval records containing full args are **admin-only
sensitive state**. Rules:

1. Full `tool_args` are stored ONLY in the approval record, never in the
   general audit trail and never in unauthenticated responses
   (`GET /approvals` stays a narrow projection; `GET /tasks/{id}` and
   `GET /audit` never expose them).
2. The human preview reads full args via an authorized path only: the local
   dashboard's own SQLite connection, or the admin-gated
   `GET /approvals/{id}` endpoint (403 without `X-Admin-Token`).
3. For this MVP the record is retained for traceability after resolution.
   Production must define data minimization (redact payloads not needed for
   replay), retention/deletion windows, and encryption at rest before
   reusing this design.
4. No encryption dependencies are added for the hackathon MVP.

---

## Confirming (how to pass Gate 0)

1. Read each `⚠️ VERIFY` line and the default above it.
2. Edit any default you disagree with, in place.
3. Delete every `⚠️ VERIFY` line — `grep -n "VERIFY" specs/DECISIONS.md` must
   return nothing.
4. Commit (human runs git, per the kit):
   `git add -A && git commit -m "docs: resolve open spec decisions" && git tag gate-0`

Phase A must not begin until this file is VERIFY-free.
