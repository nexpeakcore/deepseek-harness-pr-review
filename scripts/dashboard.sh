#!/bin/bash
# Web dashboard on http://127.0.0.1:6789, kept alive by launchd (KeepAlive).
# Runs the checkout's code, like autoreview-once.sh, so `git pull` is the update.
cd "$(dirname "$0")/.."
# launchd's minimal PATH lacks the per-user bin dir; see autoreview-once.sh.
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export PYTHONPATH=src
export PYTHONUNBUFFERED=1
export DSH_SESSION_ROOT="${DSH_SESSION_ROOT:-sessions}"
exec .venv/bin/python -m src.run web
