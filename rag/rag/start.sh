#!/usr/bin/env bash
# One command to run the Operations Copilot on macOS or Linux: ./start.sh
# Creates the virtual environment, installs dependencies when requirements.txt
# changes, creates .env on first run, then starts the server.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

if [ ! -x venv/bin/python ]; then
  PY="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3 || true)"
  if [ -z "$PY" ]; then
    echo "Python 3 was not found. Install Python 3.12 (https://www.python.org/downloads/) and run ./start.sh again." >&2
    exit 1
  fi
  echo "Creating the virtual environment with $("$PY" --version)..."
  "$PY" -m venv venv
fi

if ! cmp -s requirements.txt venv/requirements.installed; then
  echo "Installing dependencies (the first run takes 5-10 minutes)..."
  venv/bin/python -m pip install --upgrade pip
  venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt venv/requirements.installed
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Put your key in OPENROUTER_API_KEY for GLM-written answers;"
  echo "without a key the copilot still works and writes template answers."
fi

echo "Starting the copilot on http://localhost:${PORT}/  (Ctrl+C to stop)"
exec venv/bin/python -m uvicorn app.main:app --port "$PORT"
