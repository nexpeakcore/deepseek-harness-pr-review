#!/usr/bin/env bash
# One-liner installer for harness-pr-review.
#
#   curl -fsSL https://raw.githubusercontent.com/nexpeakcore/deepseek-harness-pr-review/main/scripts/install.sh | bash
#
# What it does:
#   1. Finds a Python 3.10+ interpreter (prefers 3.12/3.11/3.10, falls back to
#      Homebrew on macOS; errors clearly if only 3.9 is present)
#   2. Creates an isolated venv at ~/.harness-pr-review/venv (no system Python
#      is touched, no sudo)
#   3. Upgrades pip inside the venv (old pip builds empty "UNKNOWN" wheels)
#   4. Installs/updates the package from GitHub into the venv
#   5. Symlinks `harness-pr-review` + `autoreview` into ~/.local/bin and adds
#      it to PATH (~/.zshrc / ~/.bashrc) if missing
#   6. Runs `harness-pr-review doctor`
#
# Idempotent: re-running updates to the latest version.

set -euo pipefail

REPO_URL="git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git"
INSTALL_DIR="$HOME/.harness-pr-review"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
SHORTCUTS=("harness-pr-review" "autoreview")

say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[!]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[✗]\033[0m %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. Python
say "Looking for Python 3.10+..."
PYTHON=""
for cand in python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ] && command -v python3 >/dev/null 2>&1; then
  ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")"
  case "$ver" in
    3.1[0-9]|4.*) PYTHON="python3" ;;
  esac
fi
# macOS Homebrew fallback
if [ -z "$PYTHON" ] && [ -x /opt/homebrew/bin/python3.11 ]; then
  PYTHON="/opt/homebrew/bin/python3.11"
  warn "System python3 is too old; using Homebrew python3.11"
elif [ -z "$PYTHON" ] && [ -x /opt/homebrew/bin/python3.12 ]; then
  PYTHON="/opt/homebrew/bin/python3.12"
  warn "System python3 is too old; using Homebrew python3.12"
fi
if [ -z "$PYTHON" ]; then
  fail "Python 3.10+ is required but was not found.
      Install it with: brew install python@3.11   (macOS)
                        apt install python3.11    (Debian/Ubuntu)"
fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "need 3.10+"' \
  || fail "Python $("$PYTHON" --version 2>&1) is too old; need 3.10+"
say "Using $("$PYTHON" --version 2>&1) ($PYTHON)"

# ------------------------------------------------------------ 2. venv
mkdir -p "$INSTALL_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating venv at $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

# ------------------------------------------------------------ 3. pip
say "Upgrading pip inside the venv..."
"$VENV_PY" -m pip install --quiet --upgrade pip

# ------------------------------------------------------------ 4. install
say "Installing / updating harness-pr-review (with web extras) from GitHub..."
"$VENV_PY" -m pip install --quiet --upgrade \
  "deepseek-harness-pr-review[web] @ $REPO_URL"

# ------------------------------------------------------------ 5. symlinks
mkdir -p "$BIN_DIR"
for name in "${SHORTCUTS[@]}"; do
  ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
done

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "Adding $BIN_DIR to PATH"
    rc=""
    [ -n "${ZSH_VERSION:-}" ] && rc="$HOME/.zshrc"
    [ -n "${BASH_VERSION:-}" ] && rc="$HOME/.bashrc"
    [ -z "$rc" ] && [ -f "$HOME/.zshrc" ] && rc="$HOME/.zshrc"
    [ -z "$rc" ] && [ -f "$HOME/.bashrc" ] && rc="$HOME/.bashrc"
    if [ -n "$rc" ]; then
      printf '\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$rc"
      warn "PATH updated in $rc — open a new terminal or run: export PATH=\"$BIN_DIR:\$PATH\""
    else
      warn "Add $BIN_DIR to your PATH manually"
    fi
    ;;
esac

# ------------------------------------------------------------ 6. doctor
say "Checking readiness..."
"$BIN_DIR/harness-pr-review" doctor || true

say "Done. Run 'harness-pr-review doctor' (or open a new terminal) to verify."
say "Next: gh auth login  &&  export DEEPSEEK_API_KEY=sk-..."
say "      (or export HARNESS_PROVIDER=claude to run the review on the Claude Code CLI — no API key needed)"
