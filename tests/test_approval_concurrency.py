"""Concurrent approval resolution: eight independent attempts (Phase 2; G-03).

The claim is one conditional UPDATE, so exactly one resolver can win. These
SQLite runs prove the behaviour of the application path end to end; the
*guarantee* is accepted only from the PostgreSQL tier
(tests/test_postgres_integration.py, §4.4), because SQLite serializes writers
and its rowcount behaviour under contention is not evidence of PostgreSQL
row-level semantics.
"""

from tests.child_helpers import approve_task, run_child, resolve_concurrently, \
    sqlite_env


def test_eight_independent_processes_resolve_one_approval(tmp_path):
    env = sqlite_env(tmp_path)
    task_id, approval_id, _ = approve_task(env)

    results, lines = resolve_concurrently(env, approval_id, attempts=8)

    successes = [r for r in results if r["http"] == 200]
    conflicts = [r for r in results if r["http"] == 409]
    assert len(successes) == 1, results
    assert successes[0]["status"] == "allowed_once"
    assert successes[0]["executed"] is True
    assert len(conflicts) == 7, results
    assert all("already resolved" in str(c["detail"]) for c in conflicts)

    # Exactly one external outbox effect for the whole race:
    assert len(lines) == 1, lines
    assert lines[0]["task_id"] == task_id

    # The approval stays consumed for later attempts (deny included).
    assert run_child(env, "resolve", approval_id, "allow_once")["http"] == 409
    assert run_child(env, "resolve", approval_id, "deny")["http"] == 409


def test_a_denied_approval_cannot_then_be_allowed(tmp_path):
    """Deny uses the same conditional claim, so the two directions cannot
    both win."""
    env = sqlite_env(tmp_path)
    task_id, approval_id, _ = approve_task(env)

    denied = run_child(env, "resolve", approval_id, "deny")
    assert denied["status"] == "denied"
    after = run_child(env, "resolve", approval_id, "allow_once")
    assert after["http"] == 409
    assert after["status"] is None
    from tests.child_helpers import sink_lines
    assert sink_lines(env) == []
