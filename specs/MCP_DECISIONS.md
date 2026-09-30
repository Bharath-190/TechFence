# TaskFence — MCP Extension Design Decisions (M0)

**Status: PROPOSED — awaiting M0 human gate review (no implementation code exists yet).**

Scope of this document: design decisions ONLY, per the MCP Extension Prompt
Kit phase M0. Nothing here changes `taskfence/` production code. The existing
TaskFence gateway/security engine remains the ONLY security authority; the
MCP layer is an adapter, not a second policy engine.

Repo facts these decisions are grounded in (read before writing):

- Agent-permitted boundary: `taskfence/client.py::GatewayClient` (HTTP) — the
  ONLY module agent code may import (invariant I1, enforced by
  `tests/test_no_bypass.py`).
- Existing tools (`taskfence/tools.py`): `read_file`, `query_customer_db`,
  `send_slack`, `post_external` — PRIVATE to the gateway; sinks append JSONL
  under `outbox/`.
- Asset catalog (`taskfence/registry.py`): 11 registered assets with paths,
  source systems, data groups, base labels. **No search facility exists.**
- Gateway endpoints: `POST /tasks`, `POST /tasks/{id}/tool-call`,
  `GET /tasks/{id}`, `GET /audit`, admin-gated approval endpoints.
- Approved new dependency budget: official Python `mcp` SDK only.

---

## D1 — Which MCP SDK?

- **Question:** implement the MCP protocol by hand or use an SDK?
- **Decision:** official Python `mcp` SDK (the kit's single allowed new
  dependency). Added to `requirements.txt` in M1, not before.
- **Security reason:** protocol correctness is not security-relevant code we
  should hand-roll; a maintained SDK minimizes adapter surface and review
  burden. The SDK never sees policy inputs beyond mapping tool arguments.
- **Limitation:** SDK version churn is a supply-chain dependency.
- **Pin (recorded in M1):** `mcp==2.2.0` in `requirements.txt`. Note: MCP
  SDK v2 renamed FastMCP to `mcp.server.mcpserver.MCPServer`; the adapter
  is written against the v2 API.

## D2 — Which transport?

- **Question:** stdio, SSE, or streamable HTTP for the MCP server?
- **Decision:** **stdio** for the initial local server.
- **Security reason:** stdio binds the server to one local launcher process —
  no network listener exists to authenticate or attack; the MVP's threat
  model stays "agent process on this machine," matching the existing gateway
  (which already binds localhost only).
- **Limitation:** one MCP client per server process; remote/multi-client
  serving is out of scope and would require an auth story first.

## D3 — Where does the adapter live?

- **Question:** extend `taskfence/` or add a new package?
- **Decision:** new top-level package **`mcp_gateway/`** (server + adapter
  + its own `README.md`). No file in `taskfence/` is modified by this
  extension except `taskfence/agent.py` in M2 (additive `--mcp` path).
- **Security reason:** additive isolation — the existing engine, its tests,
  and its no-bypass enforcement stay untouched; the adapter gets its own
  equivalent ban-list test (M1-T item 8) instead of weakening the existing one.
- **Limitation:** a second package to keep in sync with `taskfence` tool
  metadata; mitigated because the adapter holds no security logic of its own.

## D4 — Which boundary reaches the decision pipeline?

- **Question:** in-process imports vs the existing HTTP boundary?
- **Decision:** the adapter calls the gateway **through
  `GatewayClient`/HTTP** — the same boundary every agent already uses.
  MCP request → adapter → `GatewayClient.call_tool()` → FastAPI gateway →
  `policy/classification/lineage/audit` → real Decision → (on ALLOW only)
  the gateway itself executes the tool.
- **Security reason:** this boundary is the tested, proven path (e2e,
  no-bypass, integration suites all drive it); decisions therefore land in
  the **existing `audit_events` trail automatically** (MCP guardrail 7 is
  satisfied by construction — the adapter writes no audit code and runs no
  tools); if the gateway is down the adapter has nothing to decide with and
  fails closed (D13).
- **Limitation:** requires a running gateway process; one extra network hop
  (localhost) per call.

## D5 — MCP-facing tools (the five)

- **Question:** what is the exact MCP tool surface?
- **Decision:** expose exactly five MCP tools, mapped 1:1 onto existing
  gateway capabilities:

| MCP tool | Existing capability | Notes |
|---|---|---|
| `read_file` | `read_file` (action `read`) | returns asset content ONLY after an ALLOW; `path` argument maps to the registry asset id |
| `search_drive` | none (new, adapter-side, **discovery only**) | see D7 |
| `query_crm` | `query_customer_db` (action `query`) | rename only; same gateway request |
| `send_slack` | `send_slack` (action `send_message`) | identical |
| `post_external` | `post_external` (action `post_external`) | identical |

- **Security reason:** no new execution capability is created; four of five
  are renames over the existing registry. The adapter never imports or calls
  `taskfence.tools`.
- **Limitation:** tool coverage equals the current fake-tool set; new
  capabilities require both a gateway tool and an MCP mapping.

## D6 — `query_crm`

- **Question:** may `query_crm` wrap the existing customer-DB operation?
- **Decision:** yes — it forwards the query to `query_customer_db` through
  the gateway (`POST /tasks/{id}/tool-call`, tool `query_customer_db`).
- **Security reason:** the gateway classifies and lineage-checks the read
  exactly as today; `customer_db` carries CUSTOMER_DATA/PII/FINANCIAL and
  outbound use of results stays gated downstream (conservative taint).
- **Limitation:** query semantics are the fake DB's keyword match — no SQL,
  no real CRM.

## D7 — `search_drive`: discovery, not permission

- **Question:** the registry has no search; how is `search_drive` provided
  without creating a bulk-authorization bypass?
- **Decision:** `search_drive` is implemented **adapter-side over registry
  metadata only** (`taskfence.registry.ASSETS`: ids, relative paths, source
  systems, data groups). It returns **candidate descriptors, never file
  contents**. Any content access requires a separate `read_file` call, which
  goes through the gateway and is individually ALLOW/APPROVE/BLOCK-evaluated.
  Calling `registry.read_asset()` from adapter code is FORBIDDEN and covered
  by an M1-T test.
- **Security reason:** implements MCP guardrail 8 literally — search is
  discovery, not permission; no wildcard or bulk authorization can exist
  because the adapter has no authority to grant any. Listing file names
  exposes nothing the registry already treats as catalog metadata.
- **Limitation:** filename-level search only (no content indexing); metadata
  listing is unauthenticated by design — acceptable for a local stdio MVP
  and documented as such (D9).

## D8 — APPROVE semantics over MCP

- **Question:** what does the MCP client receive when the gateway says
  APPROVE?
- **Decision:** the adapter returns, immediately: `decision: "APPROVE"`,
  the real `approval_id`, the gateway's `agent_message` (which already
  carries human instructions per the FIX2 work), and `executed: false`. It
  does NOT wait, poll, or retry. Resolution happens out-of-band by a human
  (dashboard/admin endpoint), exactly as today.
- **Security reason:** APPROVE is not ALLOW (MCP guardrail 9); no agent-side
  path may convert a pending approval into execution, and none is created.
- **Limitation:** an MCP agent cannot learn the post-approval outcome in the
  same call; a later re-request yields ALLOW-once semantics or a re-APPROVE,
  matching existing behavior.

## D9 — Authentication

- **Question:** does the MCP server get its own auth layer?
- **Decision:** no separate authentication feature in this milestone. The
  server runs **locally over stdio**; trust boundary = local machine user.
  The gateway's admin token remains server-side: the MCP adapter is
  agent-side and NEVER holds `TASKFENCE_ADMIN_TOKEN` (existing invariant).
- **Security reason:** adding auth to a stdio local pipe adds ceremony, not
  security, in the MVP threat model; keeping the admin token out of the
  adapter preserves the approval-authority split.
- **Limitation:** anyone/local process that can launch the MCP server can
  request gateway evaluations; production would need real authentication —
  recorded as a known limitation, out of scope.

## D10 — Ollama's role

- **Question:** does Ollama become an MCP server?
- **Decision:** no. Ollama/Qwen stays a model client. In M2 the TaskFence
  agent grows an MCP client mode (`--mcp`) that discovers the five tools
  dynamically and routes every model tool-call through the MCP server; the
  existing `--scripted` and direct-Ollama paths continue unchanged.
- **Security reason:** the model only ever chooses tools; the gateway still
  decides WHETHER (kit core principle). Making the model an MCP server would
  invert authority for zero benefit.
- **Limitation:** tool schemas must be converted MCP → Ollama format in the
  agent (M2); schema drift is a mapping bug risk, covered by M2-T tests.

## D11 — Real Slack integration (M4)

- **Question:** which real external integration, and how?
- **Decision:** exactly one Slack **incoming webhook**, activated only when
  `TASKFENCE_REAL_SLACK_WEBHOOK` is explicitly configured; implemented with
  the existing `httpx` (no Slack SDK). Fake sinks remain the default. The
  webhook POST is a gateway-executed sink path like any other: ALLOW → one
  HTTP POST; BLOCK → zero; APPROVE → zero until resolved. The URL is never
  hardcoded, logged, or placed in audit payloads.
- **Security reason:** one integration, one flag, one gateway path — no
  second security check and no side channel that bypasses policy.
- **Limitation:** single hardcoded destination semantics (one webhook);
  no Slack SDK features (threads, authed users).

## D12 — MCP session → TaskFence task mapping

- **Question:** MCP tool calls carry no user task text; how do calls map to
  TaskFence tasks without inventing a second task model?
- **Decision:** explicit, stateless threading. Every protected MCP tool call
  carries either `task_id` (existing gateway task) or `task_text` (the
  adapter creates the task via the existing `POST /tasks` and uses the
  returned `task_id`). Tool responses echo the `task_id` so clients thread
  it forward. A call with neither fails closed. The adapter keeps no
  authoritative task state — the gateway owns tasks; in practice one MCP
  session = one TaskFence task, but nothing enforces or trusts that.
- **Security reason:** contracts remain derived from user task text ONLY
  (existing invariant); the adapter cannot choose or mutate scope, and
  statelessness prevents session confusion between tasks.
- **Limitation:** slightly chattier protocol (clients re-send `task_id`);
  a misbehaving client could interleave calls across tasks — which is safe
  because every call is independently evaluated against its own contract.

## D13 — Fail-closed behavior

- **Question:** what happens when no real TaskFence Decision can be obtained
  (gateway down, timeout, non-200, unparseable body)?
- **Decision:** the adapter returns a structured MCP error result stating
  the gateway could not be reached, with NO outcome field and NO local
  execution. It never fabricates ALLOW/APPROVE/BLOCK, never retries
  internally, never falls back to a local decision.
- **Security reason:** MCP guardrail 5 — fail closed; unavailability must
  never become a bypass.
- **Limitation:** gateway downtime makes the MCP tools unusable — accepted;
  that is the correct failure direction.

---

## Invariants preserved by these decisions

Agent-side direct tool access: blocked (D4 — the adapter is just another
GatewayClient consumer). Deterministic policy: untouched (no decision logic
in `mcp_gateway/`). Immutable contracts: untouched (D12 — creation via the
existing endpoint only). Default deny: untouched. Complete audit: by
construction (D4). Lineage/derived-data inheritance: untouched. Human
approval: D8 — approval authority stays server-side. Contracts from task
text only: D12.

## Out of scope (unchanged from the kit)

Milestone 5 production sandbox/network enforcement; any second
policy/classification/lineage/approval model; any claim that MCP prevents
direct shell/HTTP/network bypasses outside the protected tool path.
