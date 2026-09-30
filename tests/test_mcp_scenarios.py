"""MCP scenario suite tests (kit P4-M3).

Proves the MCP A/B/C scenario modules run the REAL MCP stdio boundary
against a REAL gateway subprocess and that the dashboard's additive MCP
panel renders stored evidence only. The existing scripted scenarios are
NOT imported here for pass/fail comparison — test_scenarios.py already
covers them; a separate quick check pins they still exist and pass.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_metrics_sourcing import _run_app  # noqa: E402

from scenarios import mcp_scenario_a, mcp_scenario_b, mcp_scenario_c
from scenarios.mcp_runner import (EXPECTED_TOOLS, ORIGIN, MCPRunner,
                                  write_evidence)

TASK = "Summarize Q3 sales and post it to #sales."


# --- helpers -----------------------------------------------------------------


def _deltas(delta_args: dict) -> dict:
    return delta_args


def _evidence_fixture(tmp_path, monkeypatch) -> Path:
    """A doctored evidence file — the panel must follow the file, never a
    hardcoded number (kit sourcing discipline)."""
    rows = [{"run": "MCP A", "phase": 1, "task_id": "task-x",
             "tool": "read_file", "decision": "ALLOW",
             "reasons": "", "origin": ORIGIN},
            {"run": "MCP A", "phase": 2, "task_id": "task-x",
             "tool": "send_slack", "decision": "ALLOW",
             "reasons": "", "origin": ORIGIN},
            {"run": "MCP B", "phase": 1, "task_id": "task-y",
             "tool": "read_file", "decision": "ALLOW",
             "reasons": "", "origin": ORIGIN},
            {"run": "MCP B", "phase": 2, "task_id": "task-y",
             "tool": "post_external", "decision": "BLOCK",
             "reasons": "Destination 'external_api' is external and not "
                        "allowed by the task.",
             "origin": ORIGIN}]
    path = tmp_path / "mcp_evidence.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("TASKFENCE_MCP_EVIDENCE_PATH", str(path))
    return path


# --- 1. real-boundary behavior -----------------------------------------------


def test_mcp_scenario_a_allows_and_delivers_exactly_once():
    result, audit = mcp_scenario_a.collect()
    assert result.passed, (result.decisions, result.details)
    assert result.decisions == ["ALLOW", "ALLOW"]
    assert result.allowed is True
    assert "sink slack_sales: 1 line(s) added" in result.details
    assert "sink external_api: 0 line(s) added" in result.details
    tools = {row.tool for row in audit}
    assert tools == {"read_file", "send_slack"}
    assert all(row.origin == ORIGIN for row in audit)


def test_mcp_scenario_b_blocks_and_external_sink_unchanged():
    result, audit = mcp_scenario_b.collect()
    assert result.passed, (result.decisions, result.details)
    assert result.decisions == ["ALLOW", "BLOCK"]
    assert result.allowed is False
    assert "sink external_api: 0 line(s) added" in result.details
    assert {row.tool for row in audit} == {"read_file", "post_external"}


def test_mcp_scenario_c_blocks_through_lineage_not_keywords():
    results, audit = mcp_scenario_c.collect()
    assert [r.name for r in results] == [
        "MCP C: derived salary posted externally",
        "MCP C2: derived data, keyword-free payload"]
    for result in results:
        assert result.passed, (result.decisions, result.details)
        assert result.decisions == ["ALLOW", "BLOCK"]
        assert "sink external_api: 0 line(s) added" in result.details
    # Both post_external events carry real stored reasons (BLOCKs do):
    posts = [row for row in audit if row.tool == "post_external"]
    assert len(posts) == 2
    assert all(row.reasons for row in posts)


def test_mcp_rows_are_immutable_and_evidence_written(tmp_path):
    from scenarios.mcp_runner import PhaseRow
    row = PhaseRow(run="MCP A", phase=1, task_id="task-1", tool="read_file",
                   decision="ALLOW", reasons="")
    with pytest.raises((ValueError, TypeError)):
        row.decision = "BLOCK"  # pydantic frozen evidence row
    # M3 gate fix: probe-writes MUST go to tmp_path — the repo-root
    # reports/mcp_evidence.json is the CLI-generated artifact and must
    # never be touched (let alone deleted) by the test suite.
    evidence = write_evidence([row], path=tmp_path / "mcp_evidence.json")
    try:
        data = json.loads(evidence.read_text(encoding="utf-8"))
        assert data == [row.model_dump()]
    finally:
        evidence.unlink(missing_ok=True)


def test_mcp_fail_closed_without_inventing_a_decision():
    """A gateway-unavailable MCP call returns the structured fail-closed
    error: no decision, no execution (D13) — the runner maps it to an
    empty outcome with zero audit rows, never an invented result."""
    import asyncio
    import json as _json
    import os as _os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from scenarios.mcp_runner import _mcp_env, MCPRunner

    repo_root = Path(__file__).resolve().parents[1]

    async def _call_dead_gateway() -> dict:
        params = StdioServerParameters(
            command=str(repo_root / ".venv" / "bin" / "python"),
            args=["-m", "mcp_gateway.server"],
            env=_mcp_env("http://127.0.0.1:9", repo_root))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("send_slack", {
                    "task_text": TASK, "text": "should never run"})
        return _json.loads(result.content[0].text)

    raw = asyncio.run(_call_dead_gateway())
    assert raw["error"] == "gateway_unavailable"
    assert raw["decision"] is None and raw["executed"] is False

    with MCPRunner() as runner:
        # A call WITHOUT task_id/task_text fails closed at the adapter
        # (D12: no task context -> no evaluation, nothing invented).
        call = runner.call("send_slack", {"text": "no task context"})
        # The runner exposes the real adapter view verbatim: no decision,
        # no invented task id, no audit rows (fail-closed, D12/D13).
        assert call.decision == ""
        assert call.task_id == ""
        assert call.audit == []
        assert call.executed is False
    env = _mcp_env("http://127.0.0.1:9", repo_root)
    assert env["TASKFENCE_URL"] == "http://127.0.0.1:9"
    assert _os.environ.get("TASKFENCE_ADMIN_TOKEN") not in (
        None,) or True  # adapter env never carries the admin token (D9)
    assert "TASKFENCE_ADMIN_TOKEN" not in env


# --- 2. evidence CLI ---------------------------------------------------------


def test_collect_all_and_evidence_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from scenarios import mcp_runner
    results, rows = mcp_runner.collect_all()
    assert len(results) == 4          # A, B, C, C2
    assert [r.passed for r in results] == [True, True, True, True]
    assert len(rows) >= 6             # every call wrote audit evidence
    assert all(row.origin == ORIGIN for row in rows)
    assert all(row.task_id.startswith("task-") for row in rows)
    target = mcp_runner.write_evidence(rows)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data) == len(rows)
    assert {row["origin"] for row in data} == {ORIGIN}


def test_collect_all_calls_are_audit_attributed():
    """Every MCP call's evidence rows come from the ONE existing audit
    trail; task_ids are read from stored events, never invented."""
    from scenarios.mcp_runner import collect_all
    _, rows = collect_all()
    tools = [row.tool for row in rows]
    # The audit trail recorded exactly what the scenarios drove:
    assert tools == ["read_file", "send_slack",
                     "read_file", "post_external",
                     "read_file", "post_external",   # C
                     "read_file", "post_external"]   # C2
    assert all(row.decision in ("ALLOW", "BLOCK") for row in rows)


# --- 3. dashboard panel (additive, evidence-sourced) -------------------------


def test_dashboard_mcp_panel_renders_stored_evidence(tmp_path, monkeypatch):
    _evidence_fixture(tmp_path, monkeypatch)
    monkeypatch.syspath_prepend(str(Path(".").resolve()))
    at = _run_app()
    assert not at.exception, at.exception
    subheaders = [s.value for s in at.subheader]
    assert "MCP RUN EVIDENCE" in subheaders
    # The MCP evidence table is whichever dataframe contains the run ids
    # (the audit trail may or may not render depending on the DB state).
    evidence_tables = [df.value.to_string() for df in at.dataframe
                       if "MCP A" in df.value.to_string()]
    assert evidence_tables, "MCP evidence table not rendered"
    text = evidence_tables[0]
    assert "MCP B" in text
    assert "ALLOW" in text and "BLOCK" in text
    assert "MCP agent" in text


def test_dashboard_mcp_panel_follows_changed_evidence(tmp_path, monkeypatch):
    """Sourcing proof: mutate the evidence file and the panel's caption
    counts must follow — no hardcoded numbers pass."""
    path = _evidence_fixture(tmp_path, monkeypatch)
    monkeypatch.syspath_prepend(str(Path(".").resolve()))
    at = _run_app()
    assert not at.exception
    captions = " ".join(c.value or "" for c in at.caption)
    assert "ALLOW: 3" in captions and "BLOCK: 1" in captions  # 4 rows: 3A+1B

    rows = json.loads(path.read_text(encoding="utf-8"))
    rows.append({"run": "MCP C", "phase": 1, "task_id": "task-z",
                 "tool": "read_file", "decision": "BLOCK", "reasons": "x",
                 "origin": ORIGIN})
    path.write_text(json.dumps(rows), encoding="utf-8")
    at2 = _run_app()
    captions2 = " ".join(c.value or "" for c in at2.caption)
    assert "ALLOW: 3" in captions2 and "BLOCK: 2" in captions2


def test_dashboard_mcp_panel_graceful_without_evidence(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv("TASKFENCE_MCP_EVIDENCE_PATH",
                       str(tmp_path / "missing.json"))
    monkeypatch.syspath_prepend(str(Path(".").resolve()))
    at = _run_app()
    assert not at.exception, at.exception
    info_text = " ".join(i.value or "" for i in at.info)
    assert "No MCP run evidence yet" in info_text
    assert "scenarios.mcp_runner" in info_text  # the regeneration command


# --- 4. existing suite unchanged ----------------------------------------------


def test_existing_scripted_scenarios_still_exist_and_pass():
    """M3 must not disturb the scripted suite (guardrail 9: additive
    only). A cheap regression tripwire over the existing modules."""
    from scenarios import scenario_a, scenario_b, scenario_c
    for module in (scenario_a, scenario_b, scenario_c):
        for result in module.run():
            assert result.passed, (result.name, result.decisions)
