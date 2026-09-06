#!/usr/bin/env bash
# VERIFIED - one-shot launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
python -m verified.cli models
python -m verified.cli doctor
echo; echo "Starting the UI at http://127.0.0.1:8000  (Ctrl+C to stop)"
python -m verified.cli serve
