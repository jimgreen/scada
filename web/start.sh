#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
if [ -n "${SCADA_PYTHON:-}" ]; then
    exec "$SCADA_PYTHON" -B -m web.server "$@"
fi
if [ -x "$PWD/.venv/bin/python" ]; then
    exec "$PWD/.venv/bin/python" -B -m web.server "$@"
fi
exec python3 -B -m web.server "$@"
