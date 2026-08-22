#!/bin/bash
# One-shot auto review pass, launched by launchd every 2 minutes.
# Loads .env (DEEPSEEK_API_KEY etc.) if present, so the key never sits in the plist.
cd "$(dirname "$0")/.."
# launchd hands this job a minimal PATH (see the plist), which does not include
# the per-user bin dir that npm, pipx and the Claude Code installer all write
# to. Without this the claude backend fails at phase 2 with "not found on PATH"
# while the very same review works from an interactive shell.
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export PYTHONPATH=src
exec .venv/bin/python -m src.autoreview --once
