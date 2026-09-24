"""Human-readable decision explanations (spec §30; kit Prompt A3).

Produces exactly the BLOCKED / Source / Destination / Task / Why / Lineage
layout of spec §30, plus ALLOW and APPROVE equivalents. Pure formatting —
no policy logic (the dashboard and agent-facing message both consume this).

Lineage is a list of node ids, earliest first, e.g.
["employee_salary", "salary_summary", "external_api"].
"""

from taskfence.models import Decision, FlowRequest, TaskContract

_OUTCOME_TITLES = {
    "ALLOW": "ALLOWED",
    "APPROVE": "NEEDS APPROVAL",
    "BLOCK": "BLOCKED",
}


def _arrow_chain(lineage_path: list[str]) -> str:
    if not lineage_path:
        return "(not tracked)"
    return " -> ".join(lineage_path)


def explain(decision: Decision, request: FlowRequest, contract: TaskContract,
            lineage_path: list[str] | None = None) -> str:
    title = _OUTCOME_TITLES[decision.outcome]
    task = contract.purpose
    lines: list[str] = [title, ""]

    if decision.outcome == "ALLOW":
        lines += [
            f"Source: {request.source}",
            f"Destination: {request.destination}",
            f"Task: {task}",
            "",
            "Why:",
            "Flow is within task scope.",
        ]
    else:
        lines += [
            f"Source: {request.source}",
            f"Destination: {request.destination}",
            f"Task: {task}",
            "",
            "Why:",
        ]
        if decision.reasons:
            lines += [f"- {reason}" for reason in decision.reasons]
        else:  # defensive; policy always attaches at least one reason
            lines.append("- Not justified by the authorized task.")
        if decision.outcome == "APPROVE":
            lines += [
                "",
                "This flow may be legitimate but needs human confirmation.",
                "Status: waiting for approval.",
            ]

    lines += ["", f"Lineage: {_arrow_chain(lineage_path or [])}"]
    return "\n".join(lines)
