#!/usr/bin/env bash
# Run Harmony from the source tree without installing it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$here/.venv/bin/python"

if [[ ! -x "$py" ]]; then
    echo "No venv found at $here/.venv" >&2
    echo "Create one with: python3 -m venv --system-site-packages .venv" >&2
    exit 1
fi

# PyGObject comes from system site-packages; verify it before GTK errors get cryptic.
if ! "$py" -c "import gi" 2>/dev/null; then
    echo "PyGObject is not importable from $py." >&2
    echo "The venv must be created with --system-site-packages." >&2
    exit 1
fi

exec env PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" "$py" -m harmony "$@"
