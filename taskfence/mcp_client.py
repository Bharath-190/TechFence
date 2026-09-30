"""MCP client plumbing for the TaskFence agent (MCP extension kit, Person 3 / M2).

Connects the agent to the local `mcp_gateway` stdio server and converts
between MCP and the agent's world:

- tool DISCOVERY is dynamic (tools/list at connect time — never a hardcoded
  list, kit M2 requirement 4);
- tool schemas are converted MCP -> Ollama function format (requirement 5);
- every protected tool call travels MCP -> adapter -> TaskFence gateway;
  this module NEVER imports taskfence.tools/policy/lineage/audit (the same
  ban tests/test_no_bypass.py enforces for the agent — invariant I1).

Fail-closed rule (adapter D13 mirrored here): if the MCP server cannot be
started or offers no tools, connecting raises McpConnectionError — the
agent never silently falls back to the direct-Ollama path.

Usage (async, because MCP stdio is async):
    async with McpToolBridge() as bridge:
        specs = bridge.ollama_tool_specs()
        result = await bridge.call_tool(task_id, "send_slack", {...})
"""

import asyncio
import json
import os
import sys
from types import TracebackType

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_COMMAND = sys.executable or "python3"
SERVER_ARGS = ["-m", "mcp_gateway.server"]
SERVER_ENV_KEYS = ("PATH", "HOME", "PYTHONPATH", "TASKFENCE_URL",
                   "TASKFENCE_OLLAMA_URL")


class McpConnectionError(RuntimeError):
    """The MCP server could not be started or offered no tools."""


def _server_env() -> dict:
    return {key: os.environ[key] for key in SERVER_ENV_KEYS
            if key in os.environ}


class McpToolBridge:
    """Owns the MCP stdio session against `mcp_gateway.server`.

    Tool discovery happens once at __aenter__; every call_tool afterwards
    goes over that session. Nothing here decides outcomes — the JSON the
    adapter returns already carries the gateway's real decision.
    """

    def __init__(self, command: str | None = None,
                 args: list[str] | None = None) -> None:
        self._command = command or SERVER_COMMAND
        self._args = args or SERVER_ARGS
        self._streams = None
        self._session: ClientSession | None = None
        self._tools: dict[str, dict] = {}

    async def __aenter__(self) -> "McpToolBridge":
        params = StdioServerParameters(
            command=self._command, args=self._args, env=_server_env())
        self._streams = stdio_client(params)
        read, write = await self._streams.__aenter__()
        try:
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            listed = await self._session.list_tools()
            self._tools = {
                tool.name: (tool.description or "")
                for tool in listed.tools
            }
        except BaseException:
            await self._cleanup()
            raise
        if not self._tools:
            await self._cleanup()
            raise McpConnectionError(
                "the TaskFence MCP server offered no tools — failing closed")
        return self

    async def __aexit__(self, exc_type, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        session, self._session = self._session, None
        streams, self._streams = self._streams, None
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except BaseException:
                pass
        if streams is not None:
            try:
                await streams.__aexit__(None, None, None)
            except BaseException:
                pass

    # -- discovery -----------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def ollama_tool_specs(self) -> list[dict]:
        """MCP input schemas -> Ollama `tools` format (requirement 5).
        Generated from the server's tools/list — never hardcoded here."""
        specs: list[dict] = []
        for name in sorted(self._tools):
            description = self._tools[name]
            entry: dict[str, Any] = {
                "type": "function",
                "function": {"name": name,
                             "description": description or name,
                             "parameters": {"type": "object",
                                            "properties": {},
                                            "required": []}},
            }
            specs.append(entry)
        return specs

    # -- protected calls -----------------------------------------------------

    async def call_tool(self, task_id: str, tool: str,
                        args: dict | None = None) -> dict:
        """One protected call through MCP. Returns the adapter's parsed JSON:
        decision / executed / result / approval_id, or a fail-closed error
        dict when the gateway was unreachable. Unknown tools are refused
        HERE (not sent to the gateway) — discovery defines the surface."""
        if tool not in self._tools:
            raise McpConnectionError(
                f"tool {tool!r} was not offered by the MCP server")
        session = self._session
        if session is None:
            raise McpConnectionError("MCP session is not open")
        result = await session.call_tool(tool, {"task_id": task_id,
                                                **(args or {})})
        text = result.content[0].text if result.content else "{}"
        return json.loads(text)


def run_async(coro):
    """Sync entry point for main(): run one async MCP session to completion."""
    return asyncio.run(coro)
