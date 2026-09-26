"""Scenario runner + honest results report (spec §33, §34; kit Prompt G3).

Runs every scenario through the real gateway and writes reports/results.md:
- unauthorized flows intercepted X/Y
- legitimate flows allowed X/Y (false positives listed, never hidden)
- a line stating this is a controlled MVP test criterion, not a claim of
  universal security
- the "100%" headline is printed ONLY if it is actually true.

Usage: python -m scenarios.run_all
"""

from pathlib import Path

from scenarios import scenario_a, scenario_b, scenario_c, scenario_d
from scenarios import scenario_e, scenario_f, scenario_g
from scenarios.harness import ScenarioResult

REPORT_PATH = Path("reports/results.md")

ALL_SCENARIOS = [scenario_a, scenario_b, scenario_c, scenario_d,
                 scenario_e, scenario_f, scenario_g]


def collect() -> dict:
    results: list[ScenarioResult] = []
    for module in ALL_SCENARIOS:
        results.extend(module.run())
    unauthorized = [r for r in results if r.kind == "unauthorized"]
    legitimate = [r for r in results if r.kind == "legitimate"]
    metrics = {
        "results": [r.__dict__ for r in results],
        "unauthorized_total": len(unauthorized),
        "unauthorized_intercepted": sum(1 for r in unauthorized if r.passed),
        "legitimate_total": len(legitimate),
        "legitimate_allowed": sum(1 for r in legitimate if r.allowed),
        "legitimate_passed": sum(1 for r in legitimate if r.passed),
        "false_positives": [r.name for r in legitimate
                            if not r.allowed],
        "controls": [r.name for r in results if r.kind == "control"],
    }
    metrics["all_unauthorized_intercepted"] = (
        metrics["unauthorized_total"] > 0
        and metrics["unauthorized_intercepted"]
        == metrics["unauthorized_total"])
    return metrics


def render_report(metrics: dict) -> str:
    lines = ["# TaskFence — Scenario Results", ""]
    lines += ["| Scenario | Kind | Expectation | Observed | Verdict |",
              "|---|---|---|---|---|"]
    for row in metrics["results"]:
        verdict = "PASS" if row["passed"] else "FAIL"
        observed = ", ".join(row["decisions"]) or "-"
        lines.append(f"| {row['name']} | {row['kind']} | "
                     f"{row['expectation']} | {observed} | {verdict} |")
    lines += [""]

    u_total, u_pass = (metrics["unauthorized_total"],
                       metrics["unauthorized_intercepted"])
    l_total, l_allow = (metrics["legitimate_total"],
                        metrics["legitimate_allowed"])
    lines.append(f"Unauthorized flows intercepted: "
                 f"**{u_pass}/{u_total}**")
    lines.append(f"Legitimate flows allowed: **{l_allow}/{l_total}**")
    if metrics["all_unauthorized_intercepted"]:
        lines += ["", "## ✅ 100% of defined unauthorized flows "
                      "intercepted in the controlled test suite"]
    if metrics["false_positives"]:
        lines += ["", "### False positives (disclosed, not hidden)"]
        for name in metrics["false_positives"]:
            lines.append(f"- {name}")
        lines.append("Known cause: conservative lineage taint "
                     "(DECISIONS §4) — accepted for the MVP and disclosed "
                     "here per the kit's honesty rule.")
    if metrics["controls"]:
        lines += ["", "### Controls"]
        for name in metrics["controls"]:
            lines.append(f"- {name}")
    lines += ["",
              "This is a controlled MVP test criterion, not a claim of "
              "universal security. TaskFence limits the impact of "
              "manipulated agents and intercepts defined unauthorized data "
              "flows; it does not prevent all AI attacks or eliminate "
              "prompt injection."]
    return "\n".join(lines) + "\n"


def main() -> dict:
    metrics = collect()
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(render_report(metrics), encoding="utf-8")
    header = (f"{'Scenario':<52} {'Verdict':<7}")
    print(header)
    print("-" * len(header))
    for row in metrics["results"]:
        print(f"{row['name'][:51]:<52} "
              f"{'PASS' if row['passed'] else 'FAIL':<7}")
    print("-" * len(header))
    print(f"Unauthorized intercepted: "
          f"{metrics['unauthorized_intercepted']}/"
          f"{metrics['unauthorized_total']}")
    print(f"Legitimate allowed:       "
          f"{metrics['legitimate_allowed']}/{metrics['legitimate_total']}")
    print(f"False positives:          "
          f"{metrics['false_positives'] or 'none'}")
    print(f"\nReport written to {REPORT_PATH}")
    return metrics


if __name__ == "__main__":
    main()
