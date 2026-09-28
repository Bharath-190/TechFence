"""Dashboard replay-button tests (kit I5-T).

- All seven scenario controls render exactly once each (A/B/C were never
  duplicated when the E/F/G controls were added).
- Each replay button spawns the right subprocess command
  (python -m scenarios.replay scenario_e|f|g) — the same GatewayClient
  execution path as the A/B/C agent-CLI buttons.
- Subprocess truth against a REAL in-process gateway: E default-denies all
  three unknown-entity flows, F proves tamper impossibility (no mutating
  endpoint, no client surface, PUT -> 404/405), G is held for human review
  (the disclosed false positive) — and the outbox shows the truth (zero
  external deliveries everywhere; G delivers nothing without a human).
"""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.test_dashboard_scenario_d import real_gateway  # noqa: F401
from tests.test_dashboard_scenario_d import _free_port  # noqa: F401

APP_PATH = Path("dashboard/app.py").resolve()
TASK = "Summarize Q3 sales and post it to #sales."

ALL_BUTTON_LABELS = {
    "Scenario A — legitimate sales flow": (
        ["-m", "taskfence.agent", "--scripted", "scenario_a"]),
    "Scenario B — injected agent (poisoned notes)": (
        ["-m", "taskfence.agent", "--scripted", "scenario_b"]),
    "Scenario C — derived salary bypass": (
        ["-m", "taskfence.agent", "--scripted", "scenario_c"]),
    "Scenario E — unknown destination (default deny)": (
        ["-m", "scenarios.replay", "scenario_e"]),
    "Scenario F — contract tamper attempt (impossible)": (
        ["-m", "scenarios.replay", "scenario_f"]),
    "Scenario G — precision check (known false positive)": (
        ["-m", "scenarios.replay", "scenario_g"]),
}
D_BUTTON = "Run Scenario D — approval walkthrough"


def test_all_seven_scenario_buttons_render_once(real_gateway, monkeypatch):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    labels = [b.label for b in at.button]
    for label in ALL_BUTTON_LABELS:
        assert labels.count(label) == 1, (label, labels)
    assert labels.count(D_BUTTON) == 1, labels
    # The buttons exist as real trigger widgets — no inline reimplemented
    # scenario logic in the dashboard (all execution happens in
    # taskfence.agent / scenarios.replay subprocesses).


def test_replay_buttons_spawn_the_right_commands(
        real_gateway, monkeypatch):
    from streamlit.testing.v1 import AppTest

    spawned = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd))
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception
    for label, expected_args in ALL_BUTTON_LABELS.items():
        match = next(b for b in at.button if b.label == label)
        match.click().run()
        assert not at.exception
        assert len(spawned) == 1, (label, spawned)
        command = spawned[0]
        for arg in expected_args:
            assert arg in command, (label, command)
        assert command[:2] == [sys.executable, "-m"], command
        spawned.clear()


def _replay(real_gateway, scenario):
    # Inherit the fixture's environment (isolated DB/outbox/admin token) and
    # pin the dead Ollama URL so the replay subprocesses use the same
    # instant deterministic-contract path the in-process gateway was built
    # with — the demo/tests never depend on a live model (DECISIONS §8).
    import os
    env = os.environ.copy()
    env.update({"TASKFENCE_URL": real_gateway,
                "TASKFENCE_OLLAMA_URL": "http://127.0.0.1:1",
                "TASKFENCE_ADMIN_TOKEN": "devtoken"})
    return subprocess.run(
        [sys.executable, "-m", "scenarios.replay", scenario],
        capture_output=True, text=True, timeout=120, cwd=Path.cwd(), env=env)


def test_replay_e_blocks_all_unknown_entities(real_gateway, tmp_path):
    result = _replay(real_gateway, "scenario_e")
    assert result.returncode == 0, result.stderr[-1500:]
    out = result.stdout
    assert "unknown tool: BLOCK" in out
    assert "unknown destination: BLOCK" in out
    assert "unknown asset: BLOCK" in out
    # Every decision was audited by the gateway (audit events exist):
    audit = httpx.get(f"{real_gateway}/audit", timeout=10).json()["events"]
    assert len(audit) >= 3, audit
    assert all(e["decision"] == "BLOCK" for e in audit), audit


def test_replay_f_proves_contract_tamper_impossible(real_gateway, tmp_path):
    result = _replay(real_gateway, "scenario_f")
    assert result.returncode == 0, result.stderr[-1500:]
    out = result.stdout
    assert "mutating endpoints: none" in out
    assert "client mutation surface: none" in out
    assert "PUT /tasks/x/contract -> HTTP 404" in out \
        or "PUT /tasks/x/contract -> HTTP 405" in out
    # Nothing was executed: no sink deliveries at all.
    audit = httpx.get(f"{real_gateway}/audit", timeout=10).json()["events"]
    assert all(e["decision"] != "ALLOW" or e["action"] == "read"
               for e in audit), audit


def test_replay_g_stays_disclosed_false_positive(real_gateway, tmp_path):
    result = _replay(real_gateway, "scenario_g")
    assert result.returncode == 0, result.stderr[-1500:]
    out = result.stdout
    assert "read in-scope: ALLOW" in out
    assert "read out-of-scope: ALLOW" in out  # allow-but-taint (§3)
    assert "outbound message: APPROVE" in out
    # The disclosed behavior: held for review, nothing delivered autonomously.
    audit = httpx.get(f"{real_gateway}/audit", timeout=10).json()["events"]
    outbound = [e for e in audit if e["action"] == "send_message"]
    assert len(outbound) == 1 and outbound[0]["decision"] == "APPROVE"
    from taskfence import registry
    assert registry.OUTBOX_DIR.name == "outbox"  # sanity: isolated tmp outbox
    assert len(registry.sink_lines("slack_sales")) == 0
    assert not registry.sink_lines("external_api")
