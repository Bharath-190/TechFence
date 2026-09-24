"""Tests for taskfence/registry.py and the data/ tree (kit Prompt B1)."""

import json
from pathlib import Path

from taskfence import registry

DATA_DIR = Path("data")


def test_every_file_under_data_is_registered():
    files = {str(p.relative_to(DATA_DIR)) for p in registry.data_files()
             if p.name != "README.txt"}
    registered = {entry[0] for entry in registry.ASSETS.values()}
    assert files == registered, files ^ registered


def test_every_registered_file_exists():
    for asset_id in registry.asset_ids():
        assert registry.asset_path(asset_id).is_file(), asset_id


def test_assets_use_the_expected_ids():
    assert set(registry.asset_ids()) == {
        "sales_report_q3", "sales_report_q2", "internal_strategy",
        "public_company_info", "public_press_release", "meeting_notes",
        "meeting_notes_poisoned", "customer_db", "employee_salary",
        "source_code", "deploy_notes"}


def test_poisoned_file_is_present_and_carries_the_injection():
    text = registry.read_asset("meeting_notes_poisoned")
    low = text.lower()
    assert "customer_db" in low and "external" in low
    assert "ignore" in low  # injection marker per spec §39 Phase 6


def test_reset_outbox_empties_both_sinks():
    registry.reset_outbox()
    slack = registry.OUTBOX_DIR / registry.SINKS["slack_sales"]
    external = registry.OUTBOX_DIR / registry.SINKS["external_api"]
    slack.write_text('{"pre": true}\n', encoding="utf-8")
    external.write_text('{"pre": true}\n', encoding="utf-8")
    registry.reset_outbox()
    assert slack.read_text() == "" and external.read_text() == ""


def test_customer_db_is_valid_json_with_fake_personal_data():
    payload = json.loads(registry.read_asset("customer_db"))
    customers = payload["customers"]
    assert len(customers) == 30
    assert all(c["email"].endswith(".invalid") for c in customers)
    assert all(c["phone"].startswith(("8", "9")) for c in customers)


def test_salary_file_is_fake_but_classifier_relevant():
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(registry.read_asset("employee_salary"))))
    assert len(rows) == 20
    assert all(r["email"].endswith(".invalid") for r in rows)
    assert all(r["monthly_salary_ctc"].isdigit() for r in rows)


def test_no_real_looking_personal_data():
    for path in registry.data_files():
        text = path.read_text(encoding="utf-8")
        assert "gmail.com" not in text and "outlook.com" not in text \
            and "@corp.com" not in text, path


def test_resolve_round_trip():
    info = registry.resolve("customer_db")
    assert info["data_group"] == "customer_db"
    assert "CUSTOMER_DATA" in info["base_labels"]
    assert registry.resolve("no_such_asset") is None
