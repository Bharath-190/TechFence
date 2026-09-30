"""TaskFence MCP Gateway (MCP extension kit, Person 1 / M1).

A local stdio MCP server that adapts five tools onto the EXISTING TaskFence
gateway. It is an adapter, not a security engine: every protected call is
routed through GatewayClient -> the running TaskFence gateway -> the real
policy/classification/lineage pipeline, and only the gateway's actual
ALLOW / APPROVE / BLOCK decision is reported back.

Design decisions: specs/MCP_DECISIONS.md (D1-D13). This package never
imports taskfence.policy, taskfence.lineage, taskfence.tools or
taskfence.audit (enforced by tests/test_mcp_adapter.py in M1-T); the only
taskfence modules it may touch are taskfence.client.GatewayClient (D4) and
taskfence.registry METADATA for search_drive discovery (D7 — never
registry.read_asset).
"""
