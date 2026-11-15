#!/bin/sh
# Starts the editor at http://localhost:8765 (creates the venv on first run).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q pymupdf numpy; }
command -v tesseract >/dev/null || echo "Note: install Tesseract (brew install tesseract) to edit scanned PDFs."
(sleep 1; open "http://localhost:${1:-8765}" 2>/dev/null || xdg-open "http://localhost:${1:-8765}") &
exec .venv/bin/python server.py "${1:-8765}"
