#!/usr/bin/env bash
# TaskFence one-command demo (kit Prompt H2): reset state, start gateway +
# dashboard. Uses whatever python is on PATH; prefer .venv if present.
set -euo pipefail
cd "$(dirname "$0")"

PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

export TASKFENCE_ADMIN_TOKEN="${TASKFENCE_ADMIN_TOKEN:-devtoken}"

echo "[taskfence] resetting state..."
"$PY" -c "
from taskfence import registry
registry.reset_outbox()
from pathlib import Path
for db in ('taskfence.sqlite3',):
    Path(db).unlink(missing_ok=True)
print('state reset')
"

cleanup() {
  [ -n "${GATEWAY_PID:-}" ] && kill "$GATEWAY_PID" 2>/dev/null || true
  [ -n "${DASH_PID:-}" ] && kill "$DASH_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "[taskfence] starting gateway on :8000..."
"$PY" -m uvicorn taskfence.gateway:app --port 8000 &
GATEWAY_PID=$!

sleep 2
echo "[taskfence] starting dashboard on :8501..."
"$PY" -m streamlit run dashboard/app.py --server.headless true &
DASH_PID=$!

echo
echo "  Dashboard :  http://localhost:8501"
echo "  Gateway   :  http://localhost:8000"
echo "  Scenarios :  sidebar buttons A/B/C   (admin token: devtoken)"
echo "  CLI demo  :  $PY -m taskfence.agent --task 'Summarize Q3 sales and post it to #sales.' --scripted scenario_a"
echo
echo "Press Ctrl+C to stop both."

wait
