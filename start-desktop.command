#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

for python in python3 python; do
    if command -v "$python" >/dev/null 2>&1; then
        exec "$python" app/desktop_gui.py
    fi
done

printf '%s\n' 'Python 3 is required. Install Python 3 with Tk support, then try again.' >&2
exit 1
