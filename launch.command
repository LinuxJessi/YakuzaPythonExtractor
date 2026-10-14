#!/usr/bin/env bash
# Yakuza Extractor — macOS launcher (first time: right-click → Open).
cd "$(dirname "$0")"
exec python3 gui.py "$@"
