"""Outbox-isolation regression tests (hygiene defect: pytest and the test
harness must NEVER truncate or modify the tracked repository outbox/*.jsonl).

The defect this pins: three code paths called registry.reset_outbox() while
registry.OUTBOX_DIR still pointed at the REPOSITORY outbox, silently
truncating the tracked sink files on every pytest run:

1. scenarios/harness.py Harness.__exit__ — restored OUTBOX_DIR to the repo
   path and THEN reset the outbox (teardown-ordering bug);
2. tests/test_tools.py autouse fixture — reset the repo outbox directly;
3. tests/test_registry.py test_reset_outbox_empties_both_sinks — same.

The fix routes all three through isolated temporary outbox directories.
Production behavior and run_demo.sh are untouched. These tests prove the
guarantee end-to-end: they run real pytest subprocesses against the leaky
modules and the scenario CLI, assert the tracked sinks come out
byte-identical, and restore the tracked bytes in a finally block so a red
run cannot leak dirt into the repository for the remaining tests.

Fresh clones (FIX3) do not track outbox/*.jsonl (only .gitkeep, with the
jsonl sinks gitignored), so an autouse fixture seeds missing sinks with
stable sentinel bytes before snapshotting and removes them again on
teardown — while developer checkouts with real sink files keep their
original bytes untouched. No git commands are used anywhere here.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_OUTBOX = Path("outbox").resolve()
TRACKED_SINKS = ("external_api.jsonl", "slack_sales.jsonl")

# Stable sentinel bytes for sinks missing in a clean clone: deterministic
# content so byte-identity assertions remain meaningful. Only byte-identity
# is ever asserted, never file content.
SENTINEL_BYTES = b'{"sentinel": "outbox-isolation-regression-test"}\n'

PYTEST_TARGETS = [
    "tests/test_tools.py",
    "tests/test_registry.py",
    "tests/test_scenarios.py",
]


def _snapshot() -> dict[Path, bytes]:
    return {REPO_OUTBOX / name: (REPO_OUTBOX / name).read_bytes()
            for name in TRACKED_SINKS}


def _ensure_tracked_sinks() -> dict[Path, bool]:
    """Create missing tracked-sink files with stable sentinel bytes and
    return {path: created_by_this_helper} so teardown can remove exactly
    what it created (never touching pre-existing developer files)."""
    REPO_OUTBOX.mkdir(exist_ok=True)
    created: dict[Path, bool] = {}
    for name in TRACKED_SINKS:
        path = REPO_OUTBOX / name
        if not path.exists():
            path.write_bytes(SENTINEL_BYTES)
            created[path] = True
    return created


@pytest.fixture(autouse=True)
def _seed_and_restore_missing_sinks():
    """Guarantee both sink files exist for the snapshot, then restore the
    exact pre-test filesystem state (bytes for pre-existing files, removal
    for sentinel-seeded files, and the outbox/ directory itself if it was
    created here and left empty)."""
    preexisting: dict[Path, bytes] = {}
    for name in TRACKED_SINKS:
        path = REPO_OUTBOX / name
        if path.exists():
            preexisting[path] = path.read_bytes()
    created = _ensure_tracked_sinks()
    yield
    for path in created:
        path.unlink(missing_ok=True)
    for path, data in preexisting.items():
        path.write_bytes(data)
    try:
        REPO_OUTBOX.rmdir()  # only if we created it and it is empty
    except OSError:
        pass


def _assert_untouched(before: dict[Path, bytes], context: str) -> None:
    after = _snapshot()
    changed = [path.name for path, data in before.items()
               if hashlib.sha256(after[path]).hexdigest()
               != hashlib.sha256(data).hexdigest()]
    assert not changed, (
        f"{context} mutated tracked outbox files: {changed} "
        f"(before/after sha256 mismatch)")


def _restore(snapshot: dict[Path, bytes]) -> None:
    """Heal the tracked sinks (filesystem-only, no git) so a failing run
    cannot leak dirt into the repository for the remaining tests."""
    for path, data in snapshot.items():
        path.write_bytes(data)


@pytest.mark.parametrize("target", PYTEST_TARGETS)
def test_pytest_module_never_mutates_tracked_outbox(target):
    """Running any pytest module must leave the tracked repository
    outbox/*.jsonl byte-identical."""
    before = _snapshot()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", target, "-q"],
            capture_output=True, text=True, timeout=300)
        _assert_untouched(before, f"pytest {target}")
        assert result.returncode == 0, (
            f"pytest {target} failed:\n{result.stdout[-1500:]}"
            f"\n--- stderr ---\n{result.stderr[-1500:]}")
    finally:
        _restore(before)


def test_scenario_cli_never_mutates_tracked_outbox():
    """The demo entry point (python -m scenarios.run_all) runs the real
    harness end-to-end; the tracked sinks must come out byte-identical."""
    before = _snapshot()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scenarios.run_all"],
            capture_output=True, text=True, timeout=300)
        _assert_untouched(before, "scenarios.run_all")
        assert result.returncode == 0, (
            f"scenarios.run_all failed:\n{result.stdout[-1500:]}"
            f"\n--- stderr ---\n{result.stderr[-1500:]}")
    finally:
        _restore(before)


def test_harness_lifecycle_never_mutates_tracked_outbox(tmp_path):
    """In-process Harness enter/exit must use an isolated outbox and leave
    the tracked repository sinks untouched."""
    before = _snapshot()
    try:
        from scenarios.harness import Harness
        with Harness() as harness:
            assert (harness.db_dir / "outbox" /
                    "slack_sales.jsonl").exists(), \
                "harness did not use an isolated outbox directory"
            harness.sink_count("slack_sales")  # reads the isolated sinks
        _assert_untouched(before, "Harness enter/exit")
    finally:
        _restore(before)
