"""Asset registry for the fake enterprise environment (spec §18; kit B1).

Maps every file under data/ to its asset id, source system, controlled data
group and base classification labels. The gateway (Phase C) resolves assets
through this registry to build FlowRequests; the policy engine stays pure.
Also owns the outbox reset used by tests and demos.
"""

import json
from pathlib import Path

DATA_DIR = Path("data")
OUTBOX_DIR = Path("outbox")
SINKS = {"slack_sales": "slack_sales.jsonl",
         "external_api": "external_api.jsonl"}

# Asset id -> (relative path, source system, data group, base labels)
ASSETS = {
    "sales_report_q3": ("drive/sales_report_q3.csv", "drive", "sales_reports",
                        frozenset({"CONFIDENTIAL"})),
    "sales_report_q2": ("drive/sales_report_q2.csv", "drive", "sales_reports",
                        frozenset({"CONFIDENTIAL"})),
    "internal_strategy": ("drive/internal_strategy.txt", "drive",
                          "internal_strategy", frozenset({"CONFIDENTIAL"})),
    "public_company_info": ("drive/public_company_info.txt", "drive",
                            "public_info", frozenset({"PUBLIC"})),
    "public_press_release": ("drive/public_press_release.txt", "drive",
                             "public_info", frozenset({"PUBLIC"})),
    "meeting_notes": ("drive/meeting_notes.txt", "drive", "meeting_notes",
                      frozenset({"CONFIDENTIAL"})),
    "meeting_notes_poisoned": ("drive/meeting_notes_poisoned.txt", "drive",
                               "meeting_notes", frozenset({"CONFIDENTIAL"})),
    "customer_db": ("crm/customer_db.json", "crm", "customer_db",
                    frozenset({"CUSTOMER_DATA", "PII", "FINANCIAL"})),
    "employee_salary": ("hr/employee_salary.csv", "hr", "employee_salary",
                        frozenset({"EMPLOYEE_DATA", "FINANCIAL", "PII"})),
    "source_code": ("repo/source_code.py", "repo", "source_code",
                    frozenset({"SOURCE_CODE"})),
    "deploy_notes": ("repo/deploy_notes.txt", "repo", "source_code",
                     frozenset({"CONFIDENTIAL"})),
}


def asset_ids() -> list[str]:
    return sorted(ASSETS)


def asset_path(asset_id: str) -> Path:
    return DATA_DIR / ASSETS[asset_id][0]


def read_asset(asset_id: str) -> str:
    """Return the raw text/JSON content of a registered asset."""
    path = asset_path(asset_id)
    return path.read_text(encoding="utf-8")


def resolve(asset_id: str) -> dict | None:
    """Resolve an asset id to its registry entry, or None if unknown."""
    entry = ASSETS.get(asset_id)
    if entry is None:
        return None
    rel_path, source, group, labels = entry
    return {"id": asset_id, "path": rel_path, "source": source,
            "data_group": group, "base_labels": set(labels)}


def base_labels(asset_id: str) -> set[str]:
    entry = ASSETS.get(asset_id)
    return set(entry[3]) if entry else set()


def data_files() -> list[Path]:
    """Every file under data/ (used by the integration tests)."""
    return sorted(p for p in DATA_DIR.rglob("*") if p.is_file())


def reset_outbox() -> None:
    """Empty both JSONL sinks (kit Prompt B1)."""
    OUTBOX_DIR.mkdir(exist_ok=True)
    for name in SINKS.values():
        (OUTBOX_DIR / name).write_text("", encoding="utf-8")


def sink_lines(sink: str) -> list[dict]:
    """Read back a sink's JSONL lines (tests/demo verification)."""
    path = OUTBOX_DIR / SINKS[sink]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
