#!/bin/bash
# Delete review sessions of closed/merged PRs, launched by launchd once a day.
# Same environment setup as autoreview-once.sh so `gh` and .env behave the same.
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
echo "=== $(date '+%Y-%m-%d %H:%M:%S') clean-sessions ==="
exec .venv/bin/python -m src.run clean "$@"
