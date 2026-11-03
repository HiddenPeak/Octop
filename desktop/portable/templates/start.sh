#!/usr/bin/env bash
# inhere-box green portable launcher (macOS / Linux).
# Usage:
#   ./start.sh
#   ./start.sh --home /path/to/data
#   ./start.sh --home ./data --host 0.0.0.0 --port 8088
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export INHERE_BOX_HOME="${INHERE_BOX_HOME:-${ROOT}/data}"

HOST="127.0.0.1"
PORT="8088"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home)
      [[ $# -ge 2 ]] || { echo "start.sh: --home requires a path" >&2; exit 1; }
      INHERE_BOX_HOME="$2"
      shift 2
      ;;
    --host)
      [[ $# -ge 2 ]] || { echo "start.sh: --host requires a value" >&2; exit 1; }
      HOST="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "start.sh: --port requires a value" >&2; exit 1; }
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      cat <<EOF
inhere-box green portable launcher

Usage: ./start.sh [--home DIR] [--host HOST] [--port PORT] [inhere-box run args...]

Defaults:
  INHERE_BOX_HOME / --home   ${ROOT}/data
  --host                127.0.0.1
  --port                8088

Environment:
  INHERE_BOX_HOME            User data directory (overridden by --home)
EOF
      exit 0
      ;;
    *)
      EXTRA+=("$1")
      shift
      ;;
  esac
done

export INHERE_BOX_HOME
mkdir -p "$INHERE_BOX_HOME"

PY=""
if [[ -x "${ROOT}/runtime/bin/python3" ]]; then
  PY="${ROOT}/runtime/bin/python3"
elif [[ -x "${ROOT}/runtime/bin/python" ]]; then
  PY="${ROOT}/runtime/bin/python"
else
  echo "start.sh: portable Python not found under ${ROOT}/runtime" >&2
  exit 1
fi

# launch.py adds packages/ via site.addsitedir (honours .pth / pywin32).
export PYTHONNOUSERSITE=1
unset PYTHONPATH || true

echo "[inhere-box] home=${INHERE_BOX_HOME}"
echo "[inhere-box] http://${HOST}:${PORT}"
if [[ ${#EXTRA[@]} -gt 0 ]]; then
  exec "$PY" "${ROOT}/launch.py" run --host "$HOST" --port "$PORT" "${EXTRA[@]}"
else
  exec "$PY" "${ROOT}/launch.py" run --host "$HOST" --port "$PORT"
fi
