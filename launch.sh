#!/usr/bin/env bash
# Yakuza Extractor — Linux launcher. Run: ./launch.sh
set -e
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then exec python3 gui.py "$@"; fi
if command -v python >/dev/null 2>&1; then exec python gui.py "$@"; fi
echo "Python 3 is not installed. Install it and run this launcher again."; exit 1
