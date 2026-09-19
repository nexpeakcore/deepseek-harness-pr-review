"""Codex CLI backend for headless, read-only review agents.

Codex owns its authentication, so this adapter deliberately has no API-key
handling. Phase 3 runs from the disposable PR worktree with a read-only
sandbox; the harness, not the untrusted PR, writes the resulting part file.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.claude_cli import extract_json_object

BINARY = "codex"
DEFAULT_MODEL = "gpt-5.5"
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


def build_argv(*, model: str, output_path: Path, cwd: Path | None = None) -> list[str]:
    """Build a locked-down non-interactive Codex invocation.

    Read-only is intentional: a PR is untrusted input, and the response is
    saved outside its worktree before this module validates and copies JSON.
    """
    argv = [BINARY, "exec", "--ephemeral", "--sandbox", "read-only",
            "--ignore-user-config", "--ignore-rules", "--model", model,
            "--output-last-message", str(output_path)]
    if cwd is not None:
        argv += ["--cd", str(cwd)]
    else:
        argv += ["--skip-git-repo-check"]
    return argv


def run(prompt: str, *, model: str = DEFAULT_MODEL, cwd: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS, _run=subprocess.run) -> str:
    """Run Codex and return its final response without allowing workspace writes."""
    # run.py passes the session workspace relative to the checkout root. The
    # subprocess itself uses that directory as cwd, so forwarding it unchanged
    # to `codex --cd` makes Codex resolve it a second time beneath itself.
    # Resolve once before handing the path to either process.
    cwd = cwd.resolve() if cwd is not None else None
    with tempfile.TemporaryDirectory(prefix="harness-codex-") as tmp:
        output = Path(tmp) / "final.txt"
        try:
            proc = _run(build_argv(model=model, output_path=output, cwd=cwd),
                        input=prompt, cwd=str(cwd) if cwd else tmp,
                        capture_output=True, text=True,
                        timeout=timeout)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{BINARY} CLI not found on PATH — install Codex CLI, or set "
                "HARNESS_PROVIDER to deepseek or claude. "
                f"PATH was: {os.environ.get('PATH', '(unset)')}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"{BINARY} timed out after {timeout}s") from e
        except OSError as e:
            raise RuntimeError(f"could not run {BINARY}: {e}") from e
        if not output.exists():
            detail = (proc.stderr or proc.stdout or "no final response").strip()
            raise RuntimeError(f"{BINARY} failed (exit {proc.returncode}): {detail[:300]}")
        result = output.read_text().strip()
        if proc.returncode != 0:
            raise RuntimeError(f"{BINARY} failed (exit {proc.returncode}): {result[:300]}")
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
