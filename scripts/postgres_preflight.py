"""Preflight for the PostgreSQL integration tier (AD-19, PHASE_2 §4.1).

Run this before the tier itself:

    TASKFENCE_TEST_DATABASE_URL=postgresql://... \
        python scripts/postgres_preflight.py && \
        pytest -m postgres_integration

Exit codes:
    0  the test database is configured and reachable
    2  TASKFENCE_TEST_DATABASE_URL is missing or not a PostgreSQL URL
    3  the configured database is unreachable (or the check failed)

A requested tier that cannot run must FAIL here — it never counts as a
skip and never counts as a pass.
"""

from __future__ import annotations

import os
import sys

_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def main() -> int:
    url = os.environ.get("TASKFENCE_TEST_DATABASE_URL", "").strip()
    if not url:
        print("PREFLIGHT FAIL: TASKFENCE_TEST_DATABASE_URL is not set. "
              "Point it at a dedicated PostgreSQL test database.",
              file=sys.stderr)
        return 2
    if not url.startswith(_POSTGRES_SCHEMES):
        print("PREFLIGHT FAIL: TASKFENCE_TEST_DATABASE_URL must be a "
              "PostgreSQL URL (postgresql:// or postgres://).",
              file=sys.stderr)
        return 2
    production = os.environ.get("TASKFENCE_DATABASE_URL", "").strip()
    if production and production == url:
        print("PREFLIGHT FAIL: TASKFENCE_TEST_DATABASE_URL must not be the "
              "production URL.", file=sys.stderr)
        return 2
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            database, version = conn.execute(
                "SELECT current_database(), version()").fetchone()
            conn.execute("SELECT 1").fetchone()
    except Exception as error:  # noqa: BLE001 - the failure mode is the point
        print(f"PREFLIGHT FAIL: cannot reach the configured test database: "
              f"{error}", file=sys.stderr)
        return 3
    print(f"PREFLIGHT OK: database={database!r} {version.split(' on ')[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
