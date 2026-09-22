"""Antigravity CLI (agy) backend for headless review agents.

Antigravity CLI carries its own credentials, so this adapter has no API-key
handling. Phase 3 runs from the disposable PR worktree with permissions
auto-approved for headless mode.
"""
import os
import shutil
import subprocess
from pathlib import Path

from src.claude_cli import extract_json_object

BINARY = "agy"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT_SECONDS = 1800


def available() -> bool:
    return shutil.which(BINARY) is not None


def version() -> str:
    try:
        proc = subprocess.run([BINARY, "--version"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build_argv(*, prompt: str, model: str, cwd: Path | None = None,
               effort: str | None = None) -> list[str]:
    """Build a non-interactive Antigravity CLI invocation."""
    argv = [BINARY, "--dangerously-skip-permissions", "--model", model]

    # Models like gemini-3.8-flash require --effort when no suffix is given.
    if effort is None and not any(model.endswith(s) for s in ("-low", "-medium", "-high")):
        effort = "high"

    if effort:
        argv += ["--effort", effort]

    if cwd is not None:
        argv += ["--add-dir", str(cwd)]

    argv += ["--print", prompt]
    return argv


def run(prompt: str, *, model: str = DEFAULT_MODEL, cwd: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS, effort: str | None = None,
        _run=subprocess.run) -> str:
    """Run Antigravity CLI and return its final stdout response."""
    resolved_cwd = cwd.resolve() if cwd is not None else None
    argv = build_argv(prompt=prompt, model=model, cwd=resolved_cwd, effort=effort)

    try:
        proc = _run(argv,
                    cwd=str(resolved_cwd) if resolved_cwd else None,
                    capture_output=True, text=True,
                    timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"{BINARY} CLI not found on PATH — install Antigravity CLI, or set "
            "HARNESS_PROVIDER to deepseek, claude, or codex. "
            f"PATH was: {os.environ.get('PATH', '(unset)')}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{BINARY} timed out after {timeout}s") from e
    except OSError as e:
        raise RuntimeError(f"could not run {BINARY}: {e}") from e

    detail = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"{BINARY} failed (exit {proc.returncode}): {detail[:300]}")

    result = proc.stdout.strip()
    if not result:
        raise RuntimeError(f"{BINARY} returned an empty result")
    return result


def chat(messages: list[dict], *, model: str, api_key: str = "",
         base_url: str = "", max_tokens: int | None = None,
         retries: int = 3, **_) -> str:
    """`src.llm.chat`-shaped adapter for tool-free claim extraction."""
    del api_key, base_url, max_tokens, retries
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    return run(f"{system}\n\nAnswer only from the supplied text. Return the requested JSON only.\n\n{user}",
               model=model)
