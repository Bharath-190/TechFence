#!/usr/bin/env bash
# TaskFence one-command demo (kit Prompt H2): reset state, start gateway +
# dashboard. Uses whatever python is on PATH; prefer .venv if present.
#
# Fail-fast demo guarantees (no destructive cleanup, ever):
# - if the gateway/dashboard port is already occupied, report it clearly
#   and EXIT — a dashboard must never attach to an unknown/stale gateway;
# - the gateway must prove readiness (HTTP 200 on /audit) before the
#   dashboard starts;
# - only the exact processes this script started are ever cleaned up
#   (no pkill/python sweeps).
set -euo pipefail
cd "$(dirname "$0")"

PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

export TASKFENCE_ADMIN_TOKEN="${TASKFENCE_ADMIN_TOKEN:-devtoken}"
# Demo determinism: the contract builder drafts via a local LLM when one is
# reachable (~20 s per POST /tasks on qwen3 hardware, which is what produced
# the demo recording's "Gateway unreachable: timed out" — the dashboard's
# request outlives the model stall). The demo pins the documented
# deterministic fallback contract path instead (DECISIONS §8) so every
# scenario is instant and identical to the tested behavior. To demo the
# LIVE model path instead, run the gateway manually without this pin:
#   TASKFENCE_OLLAMA_URL=http://localhost:11434 uvicorn taskfence.gateway:app --port 8000
export TASKFENCE_OLLAMA_URL="${TASKFENCE_OLLAMA_URL:-http://127.0.0.1:1}"

port_busy() {  # true when the given TCP port already has a listener
  "$PY" - "$1" <<'EOF'
import socket, sys
with socket.socket() as sock:
    sock.settimeout(0.5)
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

if port_busy 8000; then
  echo "TaskFence gateway port 8000 is already in use." >&2
  echo "Stop the existing gateway before starting the demo." >&2
  exit 1
fi
if port_busy 8501; then
  echo "TaskFence dashboard port 8501 is already in use." >&2
  echo "Stop the existing Streamlit dashboard before starting the demo." >&2
  exit 1
fi

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
  [ -n "${DASH_PID:-}" ] && kill "$DASH_PID" 2>/dev/null || true
  [ -n "${GATEWAY_PID:-}" ] && kill "$GATEWAY_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "[taskfence] starting gateway on :8000..."
"$PY" -m uvicorn taskfence.gateway:app --port 8000 &
GATEWAY_PID=$!

# The dashboard starts only after the gateway REALLY answers — the demo
# must never show "Gateway unreachable" from a half-started gateway.
echo "[taskfence] waiting for gateway readiness..."
READY=0
for _ in $(seq 1 50); do
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "TaskFence gateway exited during startup — see the log above." >&2
    exit 1
  fi
  if "$PY" - <<'EOF'
import httpx, os, sys
try:
    response = httpx.get("http://127.0.0.1:8000/audit", timeout=1.0)
    sys.exit(0 if response.status_code == 200 else 1)
except Exception:
    sys.exit(1)
EOF
  then READY=1; break; fi
  sleep 0.2
done
if [ "$READY" -ne 1 ]; then
  echo "TaskFence gateway did not become ready on :8000 in time." >&2
  exit 1
fi
echo "[taskfence] gateway ready."

echo "[taskfence] starting dashboard on :8501..."
"$PY" -m streamlit run dashboard/app.py --server.headless true &
DASH_PID=$!

echo
echo "  Dashboard :  http://localhost:8501"
echo "  Gateway   :  http://localhost:8000"
echo "  Scenarios :  sidebar buttons A-G   (admin token: ${TASKFENCE_ADMIN_TOKEN})"
echo "  CLI demo  :  $PY -m taskfence.agent --task 'Summarize Q3 sales and post it to #sales.' --scripted scenario_a"
echo
echo "Press Ctrl+C to stop both."

wait
