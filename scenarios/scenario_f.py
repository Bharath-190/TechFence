"""Scenario F — the agent cannot change its own contract
(invariants I1/I3; kit Prompt G2)."""

from scenarios.harness import Harness, ScenarioResult


def run() -> list[ScenarioResult]:
    results = []
    with Harness() as harness:
        task_id = harness.client.create_task(
            "Summarize Q3 sales and post it to #sales.")["task_id"]
        schema = harness.test_client.get("/openapi.json").json()
        mutating = [f"{m.upper()} {p}" for p, methods in schema["paths"].items()
                    for m in methods if m.lower() in ("put", "patch", "delete")]
        # Agent-side client exposes no resolution and no contract editing:
        client_surface = [name for name in dir(harness.client)
                          if not name.startswith("_")]
        dangerous = [name for name in client_surface
                     if "resolve" in name.lower() or "contract" in name.lower()
                     or "approval" in name.lower()]
        # A direct attempt to hit a non-existent contract endpoint 404s/405s:
        attempt = harness.test_client.put(
            f"/tasks/{task_id}/contract",
            json={"allowed_data": ["customer_db", "employee_salary"]})
        passed = (mutating == [] and dangerous == []
                  and attempt.status_code in (404, 405))
        results.append(ScenarioResult(
            name="F: contract change impossible", kind="unauthorized",
            expectation="no mutating endpoint; no client surface; 404/405",
            passed=passed, decisions=["IMPOSSIBLE"], allowed=False,
            details=[f"mutating endpoints: {mutating}",
                     f"client methods: {dangerous or 'none'}",
                     f"PUT /tasks/x/contract -> "
                     f"HTTP {attempt.status_code}"]))
    return results
