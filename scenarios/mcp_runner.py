"""MCP scenario runner (kit P4-M3) — real MCP boundary, real gateway.

Drives the MCP A/B/C scenarios through the REAL TaskFence MCP boundary: a
spawned `python -m mcp_gateway.server` subprocess speaking MCP stdio to a
real uvicorn TaskFence gateway (the same proven pattern as the M1-T
suite, tests/test_mcp_adapter.py). The MCP layer is an adapter, not a
second policy engine: every protected call is evaluated by the existing
gateway pipeline; this runner only OBSERVES the real decisions, the
existing audit_events trail and the outbox sinks.

Model calls are NOT needed: the "model" is the deterministic scenario
script that issues each MCP tool call (kit P4-M3: "Use deterministic
mocked-model mode for CI"). The taskfence agent M2 --mcp path is the
live-demonstration counterpart of these flows.

Attribution without touching production code: the gateway is the single
authority and writes the ONE audit trail; the runner reads /audit and
attributes new rows to the current call by row-id cursor (runs are
strictly sequential), so task_ids come from the stored events themselves.

Lifecycle: one fresh gateway subprocess + isolated CWD/outbox per runner
(guardrail: no cross-scenario pollution, repo state never touched).
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict

from scenarios.harness import ScenarioResult
from taskfence import registry
from taskfence.client import GatewayClient

TASK = "Summarize Q3 sales and post it to #sales."
ORIGIN = "MCP agent"  # exact dashboard-facing origin label (P4-M3)
EXPECTED_TOOLS = {"read_file", "search_drive", "query_crm", "send_slack",
                  "post_external"}


# --- evidence containers -----------------------------------------------------


class PhaseRow(BaseModel):
    """One immutable runtime-evidence row, straight from audit_events."""

    model_config = ConfigDict(frozen=True)  # evidence is write-once

    run: str             # scenario run label, e.g. "MCP A"
    phase: int            # 1-based step of this run
    task_id: str
    tool: str             # gateway-facing tool name
    decision: str         # ALLOW | APPROVE | BLOCK (verbatim)
    reasons: str          # stored policy reasons (deny/hold explanations)
    origin: str = ORIGIN  # "MCP agent" for every row in this kit


class SingleRunResult(NamedTuple):
    """One MCP tool call's real outcome + its own stored audit rows."""

    task_id: str                     # from the audit rows the call produced
    decision: str                    # verbatim gateway outcome
    executed: bool                   # adapter's execution flag (ALLOW only)
    approval_id: str | None          # set on APPROVE only
    agent_message: str               # verbatim agent-facing message
    result: dict | None              # tool result payload (ALLOW only)
    audit: list[dict]                # the audit_events rows this call wrote


# --- MCP boundary helpers (same pattern as tests/test_mcp_adapter.py) --------


def _mcp_env(base_url: str, repo_root: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONPATH": str(repo_root),
            "TASKFENCE_URL": base_url,
            "TASKFENCE_MCP_TIMEOUT": "10"}


def _mcp(base_url: str, tool: str, args: dict, repo_root: Path) -> dict:
    """One tool call over the real stdio boundary; returns the parsed JSON
    result text the adapter produced."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> dict:
        params = StdioServerParameters(
            command=str(repo_root / ".venv" / "bin" / "python"),
            args=["-m", "mcp_gateway.server"], env=_mcp_env(base_url,
                                                            repo_root))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
        text = result.content[0].text if result.content else ""
        return json.loads(text)

    return asyncio.run(_run())


def _list_tools(base_url: str, repo_root: Path) -> set[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> set[str]:
        params = StdioServerParameters(
            command=str(repo_root / ".venv" / "bin" / "python"),
            args=["-m", "mcp_gateway.server"], env=_mcp_env(base_url,
                                                            repo_root))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {t.name for t in (await session.list_tools()).tools}

    return asyncio.run(_run())


# --- gateway lifecycle (real uvicorn subprocess, isolated state) -------------


def _spawn_gateway(python_exe: str, cwd: Path,
                   repo_root: Path) -> tuple[subprocess.Popen, str]:
    """One real uvicorn gateway subprocess; returns (proc, base_url).

    cwd is the ISOLATED state dir (registry resolves data/ and outbox/
    relative to it), while PYTHONPATH must point at the REPO ROOT — the
    taskfence package lives there, not in the temp dir."""
    for _ in range(50):  # grab a free localhost port
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            free = sock.getsockname()[1]
            break
        finally:
            sock.close()
    env = {**os.environ,
           "PYTHONPATH": str(repo_root),
           "TASKFENCE_DB": str(cwd / "taskfence.sqlite3"),
           "TASKFENCE_ADMIN_TOKEN": "devtoken",
           "TASKFENCE_OLLAMA_URL": "http://127.0.0.1:1"}
    for inherited in ("TASKFENCE_URL", "TASKFENCE_REPORT_PATH"):
        env.pop(inherited, None)  # never inherit an outer demo's settings
    (cwd / "outbox").mkdir(parents=True, exist_ok=True)
    # The gateway resolves asset files CWD-relative (data/...). The
    # isolated cwd keeps the OUTBOX isolated; a symlink keeps the real
    # fixture files readable without copying or touching the repo.
    data_link = cwd / "data"
    if not data_link.exists():
        os.symlink(repo_root / "data", data_link)
    proc = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "taskfence.gateway:app",
         "--host", "127.0.0.1", "--port", str(free),
         "--log-level", "warning"],
        cwd=str(cwd), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{free}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("gateway subprocess died during startup")
        try:
            if httpx.get(f"{base}/audit", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("gateway subprocess never became ready")


class MCPRunner:
    """Owns one real gateway subprocess + sink probes for the MCP A/B/C
    runs. Restores CWD, outbox pointer and the subprocess on stop()."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = (Path(repo_root) if repo_root
                          else Path(__file__).resolve().parents[1])
        self.python = str(self.repo_root / ".venv" / "bin" / "python")
        self.workdir = Path(tempfile.mkdtemp(prefix="tf_mcp_scenarios_"))
        self._owns_dir = True
        self._old_cwd: Path | None = None
        self._old_outbox: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.base_url: str | None = None
        self.db: Path | None = None
        self._audit_cursor = 0  # last audit row id attributed to a call

    # lifecycle ---------------------------------------------------------

    def start(self) -> "MCPRunner":
        self.proc, self.base_url = _spawn_gateway(
            self.python, self.workdir, self.repo_root)
        self.db = self.workdir / "taskfence.sqlite3"
        self._old_cwd = Path.cwd()
        os.chdir(self.workdir)  # registry sinks resolve CWD-relative
        self._old_outbox = registry.OUTBOX_DIR
        registry.OUTBOX_DIR = self.workdir / "outbox"
        registry.reset_outbox()
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._old_outbox is not None:
            registry.OUTBOX_DIR = self._old_outbox
            self._old_outbox = None
        if self._old_cwd is not None:
            os.chdir(self._old_cwd)
            self._old_cwd = None
        if self._owns_dir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self._owns_dir = False

    def __enter__(self) -> "MCPRunner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # observation surface (stored evidence only) ------------------------

    def discover_tools(self) -> set[str]:
        return _list_tools(self.base_url, self.repo_root)

    def _sink_path(self, sink: str) -> Path:
        return self.workdir / "outbox" / registry.SINKS[sink]

    def _sink_bytes(self, sink: str) -> bytes:
        path = self._sink_path(sink)
        return path.read_bytes() if path.exists() else b""

    def sinks_before(self) -> dict[str, bytes]:
        """Byte snapshot of every sink (call before a phase)."""
        return {sink: self._sink_bytes(sink) for sink in registry.SINKS}

    def sink_deltas(self, before: dict[str, bytes]) -> dict[str, int]:
        """Lines added per sink since the snapshot (byte-level evidence)."""
        return {sink: len(self._sink_bytes(sink).splitlines())
                - len(snapshot.splitlines())
                for sink, snapshot in before.items()}

    def sink_bytes(self, sink: str) -> bytes:
        return self._sink_bytes(sink)

    def audit_events(self) -> list[dict]:
        """The existing audit trail, verbatim from the gateway."""
        response = httpx.get(f"{self.base_url}/audit", timeout=10)
        response.raise_for_status()
        return response.json()["events"]

    def mcp_call(self, tool: str, args: dict) -> dict:
        return _mcp(self.base_url, tool, args, self.repo_root)

    def call(self, tool: str, args: dict) -> SingleRunResult:
        """One protected call over the REAL MCP stdio boundary, plus the
        audit_events rows THE CALL ITSELF wrote (row-id cursor delta —
        runs are sequential, so new rows are exactly this call's)."""
        raw = self.mcp_call(tool, args)
        rows = [e for e in self.audit_events() if e["id"] > self._audit_cursor]
        self._audit_cursor = max(
            [self._audit_cursor] + [e["id"] for e in rows])
        return SingleRunResult(
            task_id=rows[0]["task_id"] if rows else "",
            decision=raw.get("decision") or "",
            executed=bool(raw.get("executed")),
            approval_id=raw.get("approval_id"),
            agent_message=raw.get("agent_message", ""),
            result=raw.get("result"),
            audit=rows)

    def run_script(self, script: list[dict],
                   task_text: str = TASK) -> list[SingleRunResult]:
        """Run a scripted flow the way a real MCP agent does (D12): create
        ONE TaskFence task via the existing GatewayClient boundary, then
        thread its task_id into EVERY MCP call. A call without task info
        fails closed by design — the runner never bypasses that."""
        task_id = GatewayClient(
            base_url=self.base_url).create_task(task_text)["task_id"]
        return [self.call(step["tool"], {"task_id": task_id,
                                         **step.get("args", {})})
                for step in script]


def phase_rows(run: str, rows: list[SingleRunResult]) -> list[PhaseRow]:
    """Flatten per-call audit evidence into phase-ordered PhaseRows (one
    trail, one origin — no second log is created anywhere)."""
    out: list[PhaseRow] = []
    for result in rows:
        for event in result.audit:
            out.append(PhaseRow(
                run=run, phase=len(out) + 1, task_id=event["task_id"],
                tool=event["tool"], decision=event["decision"],
                reasons=event.get("reasons", ""), origin=ORIGIN))
    return out


def as_scenario_result(name: str, kind: str, expectation: str, *,
                       decisions: list[str], sink_deltas: dict[str, int],
                       audit_rows: list[PhaseRow],
                       passed: bool) -> ScenarioResult:
    """Bundle a scenario's real outcome into the existing ScenarioResult
    shape (run_all/dashboard compatible), with evidence in details."""
    allowed = bool(decisions) and all(d == "ALLOW" for d in decisions)
    details = [f"{row.phase}. [{row.origin}] {row.tool} -> {row.decision}"
               + (f" ({row.reasons})" if row.reasons else "")
               for row in audit_rows]
    details += [f"sink {sink}: {delta} line(s) added"
                for sink, delta in sorted(sink_deltas.items())]
    return ScenarioResult(
        name=name, kind=kind, expectation=expectation, passed=passed,
        decisions=decisions, allowed=allowed, details=details)


# --- evidence CLI (dashboard input; same role run_all has for results.md) ----

EVIDENCE_PATH = Path("reports/mcp_evidence.json")


def write_evidence(rows: list[PhaseRow],
                   path: Path | None = None) -> Path:
    """Persist the MCP runs' stored evidence for the dashboard's additive
    panel. Display data only — never a second security artifact."""
    target = path or EVIDENCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([row.model_dump() for row in rows], indent=2),
        encoding="utf-8")
    return target


def collect_all() -> tuple[list[ScenarioResult], list[PhaseRow]]:
    """Run the MCP A/B/C scenario modules and return their results plus
    the flattened runtime evidence rows."""
    from scenarios import mcp_scenario_a, mcp_scenario_b, mcp_scenario_c

    results: list[ScenarioResult] = []
    rows: list[PhaseRow] = []
    for module in (mcp_scenario_a, mcp_scenario_b, mcp_scenario_c):
        result, phase = module.collect()
        if isinstance(result, ScenarioResult):  # A/B yield one result
            results.append(result)
        else:                                   # C yields C + C2
            results.extend(result)
        rows.extend(phase)
    return results, rows


def main() -> dict:
    """CLI: python -m scenarios.mcp_runner — run MCP A/B/C over the real
    boundary, write reports/mcp_evidence.json, print honest verdicts."""
    results, rows = collect_all()
    target = write_evidence(rows)
    header = f"{'MCP scenario':<52} {'Verdict':<7}"
    print(header)
    print("-" * len(header))
    for result in results:
        print(f"{result.name[:51]:<52} "
              f"{'PASS' if result.passed else 'FAIL':<7}")
    print("-" * len(header))
    print(f"Evidence rows written: {len(rows)} -> {target}")
    return {"results": [r.__dict__ for r in results],
            "evidence_rows": len(rows), "evidence_path": str(target)}


if __name__ == "__main__":
    main()
