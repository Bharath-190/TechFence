"""Parent-side helpers for the Phase 2 cross-process / concurrency tests.

These launch ``tests/proc_child.py`` as a fresh interpreter, so a result can
only come from the configured database — never from shared process memory.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
CHILD_SCRIPT = TESTS_DIR / "proc_child.py"
ADMIN_TOKEN = "devtoken"


def sqlite_env(tmp_path: Path) -> dict:
    """Child environment bound to one SQLite file under ``tmp_path``."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("TASKFENCE_DATABASE_URL", None)
    env["TASKFENCE_DB"] = str(tmp_path / "taskfence.sqlite3")
    env["TASKFENCE_ADMIN_TOKEN"] = ADMIN_TOKEN
    env["TASKFENCE_OLLAMA_URL"] = "http://127.0.0.1:1"
    env["TF_OUTBOX_DIR"] = str(tmp_path / "outbox")
    return env


def postgres_env(url: str, tmp_path: Path) -> dict:
    """Child environment bound to the PostgreSQL test database.

    ``TASKFENCE_DB`` is removed entirely so the only configured backend is
    PostgreSQL — a leftover SQLite path would be a conflicting-backend
    configuration error, not a silent second database.
    """
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("TASKFENCE_DB", None)
    env["TASKFENCE_DATABASE_URL"] = url
    env["TASKFENCE_ADMIN_TOKEN"] = ADMIN_TOKEN
    env["TASKFENCE_OLLAMA_URL"] = "http://127.0.0.1:1"
    env["TF_OUTBOX_DIR"] = str(tmp_path / "outbox")
    return env


def run_child(env: dict, *args: str, timeout: int = 240) -> dict:
    """Run one fresh interpreter and return its JSON payload."""
    import sys
    command = [sys.executable, str(CHILD_SCRIPT), *[str(a) for a in args]]
    process = subprocess.run(
        command, cwd=str(REPO_ROOT), env=env, capture_output=True,
        text=True, timeout=timeout)
    if process.returncode != 0:
        raise AssertionError(
            f"child {args} exited {process.returncode}\n"
            f"stdout:\n{process.stdout[-4000:]}\n"
            f"stderr:\n{process.stderr[-4000:]}")
    payload = [line for line in process.stdout.splitlines()
               if line.startswith("CHILD_JSON: ")]
    if not payload:
        raise AssertionError(
            f"child {args} printed no result\nstdout:\n{process.stdout}\n"
            f"stderr:\n{process.stderr}")
    return json.loads(payload[-1].split("CHILD_JSON: ", 1)[1])


def spawn_children(envs: list[dict], args_list: list[tuple]) -> list:
    """Start all children concurrently (independent interpreters)."""
    import sys
    processes = []
    for env, args in zip(envs, args_list):
        command = [sys.executable, str(CHILD_SCRIPT),
                   *[str(a) for a in args]]
        processes.append(subprocess.Popen(
            command, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    return processes


def collect(processes) -> list[dict]:
    """Wait for spawned children and parse their JSON payloads."""
    results = []
    for index, process in enumerate(processes):
        stdout, stderr = process.communicate(timeout=240)
        if process.returncode != 0:
            raise AssertionError(
                f"child {index} exited {process.returncode}\n"
                f"stdout:\n{stdout[-4000:]}\nstderr:\n{stderr[-4000:]}")
        payload = [line for line in stdout.splitlines()
                   if line.startswith("CHILD_JSON: ")]
        if not payload:
            raise AssertionError(f"child {index} printed no result\n"
                                 f"stdout:\n{stdout}\nstderr:\n{stderr}")
        results.append(json.loads(payload[-1].split("CHILD_JSON: ", 1)[1]))
    return results


def sink_lines(env: dict, sink: str = "slack_sales") -> list[dict]:
    """Outbox lines written by whichever child processes ran."""
    from taskfence import registry
    path = Path(env["TF_OUTBOX_DIR"]) / registry.SINKS[sink]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


def approve_task(env: dict) -> tuple[str, str, dict]:
    """Create a task whose outbound tool call requires human approval."""
    task = run_child(env, "create_task",
                     "Summarize Q3 sales and post it to #sales.")
    assert task["http"] == 200, task
    args = {"channel": "#sales",
            "text": "regional conversion summary for Q3",
            "source_assets": ["customer_db"]}
    call = run_child(env, "tool_call", task["task_id"], "send_slack",
                     json.dumps(args))
    assert call["outcome"] == "APPROVE", call
    return task["task_id"], call["approval_id"], call


def resolve_concurrently(env: dict, approval_id: str,
                         attempts: int = 8) -> tuple[list[dict], list[dict]]:
    """Eight independent interpreters resolve the same approval at once."""
    envs = [dict(env) for _ in range(attempts)]
    args_list = [("resolve", approval_id, "allow_once")] * attempts
    results = collect(spawn_children(envs, args_list))
    return results, sink_lines(env)
