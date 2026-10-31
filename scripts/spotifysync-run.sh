#!/bin/bash
# Launch SpotifySync from source using the project's virtualenv.
# Used by the desktop entry installed by install_desktop.sh.
set -e

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python spotify.py "$@"
fi

exec python3 spotify.py "$@"
