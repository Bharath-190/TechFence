"""Lineage tests (kit Prompt B3): taint, modes, persistence."""

import pytest

from taskfence.lineage import LineageTracker


@pytest.fixture()
def tracker(tmp_path):
    t = LineageTracker(db_path=tmp_path / "lin.db", mode="declared")
    yield t
    t.close()


def test_register_labels_source_node(tracker):
    tracker.register_asset("t1", "employee_salary")
    assert tracker.effective_labels("t1", "employee_salary") >= {
        "EMPLOYEE_DATA", "FINANCIAL"}


def test_multi_hop_label_inheritance(tracker):
    tracker.register_asset("t1", "employee_salary")
    tracker.derive("t1", ["employee_salary"], "average", "salary_summary")
    tracker.derive("t1", ["salary_summary"], "format", "summary_v2")
    labels = tracker.effective_labels("t1", "summary_v2")
    assert {"EMPLOYEE_DATA", "FINANCIAL"} <= labels


def test_derive_with_unknown_inputs_still_wires_edges(tracker):
    tracker.derive("t1", ["mystery_source"], "mix", "blend")
    assert "mystery_source" in tracker.ancestors("t1", "blend")


def test_conservative_mode_taints_without_declared_inputs(tmp_path):
    t = LineageTracker(db_path=tmp_path / "lin.db", mode="conservative")
    t.register_asset("t1", "employee_salary")
    # Outbound node derived from nothing sensitive:
    t.derive("t1", [], "compose", "greeting_text")
    labels = t.effective_labels("t1", "greeting_text")
    assert {"EMPLOYEE_DATA", "FINANCIAL"} <= labels
    t.close()


def test_declared_mode_does_not_taint_unrelated_node(tracker):
    tracker.register_asset("t1", "employee_salary")  # session read
    tracker.derive("t1", [], "compose", "greeting_text")
    labels = tracker.effective_labels("t1", "greeting_text")
    assert "EMPLOYEE_DATA" not in labels


def test_conservative_can_be_overridden_per_call(tmp_path):
    t = LineageTracker(db_path=tmp_path / "lin.db", mode="conservative")
    t.register_asset("t1", "employee_salary")
    labels = t.effective_labels("t1", "anything", include_session=False)
    assert "EMPLOYEE_DATA" not in labels
    t.close()


def test_path_to_matches_spec_chain(tmp_path):
    t = LineageTracker(db_path=tmp_path / "lin.db", mode="declared")
    t.register_asset("t1", "employee_salary")
    t.derive("t1", ["employee_salary"], "average", "salary_summary")
    t.record_sink("t1", "salary_summary", "external_api")
    assert t.path_to("t1", "external_api") == \
        ["employee_salary", "salary_summary", "external_api"]
    t.close()


def test_persistence_round_trip(tmp_path):
    db = tmp_path / "lin.db"
    t = LineageTracker(db_path=db, mode="declared")
    t.register_asset("t1", "employee_salary")
    t.derive("t1", ["employee_salary"], "average", "salary_summary")
    t.close()

    fresh = LineageTracker(db_path=db, mode="declared")
    assert {"EMPLOYEE_DATA", "FINANCIAL"} <= \
        fresh.effective_labels("t1", "salary_summary", include_session=False)
    fresh.close()
