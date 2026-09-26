"""Scenario suite and report honesty tests (kit Prompt G-T).

Do NOT change scenario source files from here; report bugs instead.
"""

import pytest

from scenarios import run_all, scenario_a, scenario_b, scenario_c
from scenarios import scenario_d, scenario_e, scenario_f, scenario_g


SCENARIO_MODULES = [scenario_a, scenario_b, scenario_c, scenario_d,
                    scenario_e, scenario_f, scenario_g]

# Gate G decision (kit: "decide together whether to fix or disclose it."):
# Scenario G's conservative-taint friction is DISCLOSED, not fixed, so it is
# excluded from the blanket pass assertion and covered by its own honesty test.
MUST_PASS_MODULES = [m for m in SCENARIO_MODULES if m is not scenario_g]


@pytest.mark.parametrize("module", MUST_PASS_MODULES,
                         ids=lambda m: m.__name__)
def test_scenario_module_passes(module):
    for result in module.run():
        assert result.passed, (result.name, result.decisions, result.details)


def test_scenario_g_over_block_is_disclosed_not_hidden():
    results = scenario_g.run()
    assert len(results) == 1
    result = results[0]
    assert result.kind == "legitimate"
    assert result.allowed is False       # the flow did not execute autonomously
    assert result.passed is False        # honest, not gamed to green
    assert any("disclosed" in detail.lower() or "conservative" in detail.lower()
               for detail in result.details)


def test_run_all_returns_structured_counts():
    metrics = run_all.collect()
    for key in ("unauthorized_total", "unauthorized_intercepted",
                "legitimate_total", "legitimate_allowed", "false_positives",
                "all_unauthorized_intercepted"):
        assert key in metrics
    assert metrics["unauthorized_total"] >= 5
    assert metrics["legitimate_total"] >= 2
    assert metrics["unauthorized_intercepted"] == \
        metrics["unauthorized_total"]


def test_report_written_and_contains_counts():
    metrics = run_all.main()
    report = run_all.REPORT_PATH.read_text(encoding="utf-8")
    assert (f"{metrics['unauthorized_intercepted']}/"
            f"{metrics['unauthorized_total']}") in report
    assert (f"{metrics['legitimate_allowed']}/"
            f"{metrics['legitimate_total']}") in report
    assert "controlled MVP test criterion" in report


def test_report_honesty_100_only_when_true():
    """A failed unauthorized scenario must suppress the 100% headline."""
    fake_fail = {"name": "X: broken control", "kind": "unauthorized",
                 "expectation": "BLOCK", "passed": False,
                 "decisions": ["BLOCK", "ALLOW"], "allowed": False,
                 "details": []}
    good = {"name": "A", "kind": "unauthorized", "expectation": "BLOCK",
            "passed": True, "decisions": ["BLOCK"], "allowed": False,
            "details": []}
    metrics = {
        "results": [good, fake_fail],
        "unauthorized_total": 2, "unauthorized_intercepted": 1,
        "legitimate_total": 1, "legitimate_allowed": 1,
        "legitimate_passed": 1, "false_positives": [],
        "controls": [], "all_unauthorized_intercepted": False}
    report = run_all.render_report(metrics)
    assert "100%" not in report
    assert "1/2" in report

    metrics["all_unauthorized_intercepted"] = True
    metrics["unauthorized_intercepted"] = 2
    honest = run_all.render_report(metrics)
    assert "100%" in honest


def test_false_positive_listed_not_hidden():
    metrics = run_all.collect()
    fp_names = metrics["false_positives"]
    if fp_names:  # scenario G is expected to over-block under conservative taint
        report = run_all.REPORT_PATH.read_text(encoding="utf-8")
        for name in fp_names:
            assert name in report
        assert "disclosed" in report.lower()


def test_scenario_b_sink_never_receives_data():
    with __import__("scenarios.harness", fromlist=["Harness"]).Harness() as h:
        scenario_b.run.__globals__  # module imports intact
        # Re-run inside a fresh harness and verify sink emptiness directly:
        from scenarios.harness import Harness
        from taskfence.agent import ScriptedAgent
        with Harness() as harness:
            ScriptedAgent(harness.client).run(
                "Summarize Q3 sales and post it to #sales.", "scenario_b")
            assert harness.sink_count("external_api") == 0
