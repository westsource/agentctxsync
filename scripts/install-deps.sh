#!/usr/bin/env sh
# hermes-sync client dependency bootstrap (POSIX)
# Creates <extract-dir>/venv with mcp (and zstandard for dsh) and prints the
# interpreter to register as <PYTHON>. Run once after unzipping.
set -e

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV="$ROOT/venv"

if [ -x "$VENV/bin/python" ]; then
    echo "[hermes-sync] ready: $VENV/bin/python"
    exit 0
fi

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[hermes-sync] ERROR: no python3 found (need Python 3.10+)" >&2
    exit 1
fi

echo "[hermes-sync] creating venv at $VENV ..."
"$PY" -m venv "$VENV"
echo "[hermes-sync] installing mcp + zstandard (first run needs network) ..."
"$VENV/bin/python" -m pip install mcp zstandard

echo "[hermes-sync] ready. Register this as <PYTHON>:"
echo "  $VENV/bin/python"
echo "  args: $ROOT/mcp/server.py"
exit 0
