"""Claude Code CLI as a second agent backend (headless `claude -p`).

The pipeline was written against one agent runtime — the DeepSeek Harness SDK —
but no phase actually depends on *which* agent reads the workspace. verify.py
already takes an injectable `runner` and claims.py an injectable `chat`, so a
second backend is a matter of filling those two seams rather than threading a
provider flag through every phase.

Why the CLI and not the Anthropic API directly: phase 3 needs a tool loop (read
the repo, then write findings-<axis>.json), and `claude -p` *is* that loop. It
also carries its own credentials, so a Claude subscription login is enough —
this backend adds no second API key to .env.

The workspace holds a PR from a repo we do not control, so every invocation is
narrowed the same way cordis/minimal.cordis.yml narrows the DeepSeek agent:

- `--tools` — the boundary. It sets which built-in tools *exist* for the run:
  read the code, write one part file, nothing else. `--allowedTools` is not a
  boundary and was the first thing tried here — it only pre-approves what may
  run without a prompt, so a tool left out of it still exists, and a user
  settings rule can approve it anyway. `--tools ""` removes every tool, which
  is what phase 2 runs with. Both are passed: --tools decides what exists,
  --allowedTools keeps what exists from stopping to ask.
- `--safe-mode` — the checked-out repo's own CLAUDE.md, hooks, skills, plugins
  and custom agents are not loaded. Without it a PR could ship a CLAUDE.md and
  steer the reviewer that is meant to be reviewing it.
- `--setting-sources user` + `--strict-mcp-config` — same reasoning for a
  .claude/settings.json or .mcp.json committed inside the workspace.
- cwd is the workspace, so the Write tool lands the part file exactly where
  verify.read_part() looks for it.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BINARY = "claude"
DEFAULT_MODEL = "sonnet"

# Read the code, write one findings part. Nothing else exists for the run.
AGENT_TOOLS = ("Read", "Grep", "Glob", "Write")
# Phase 2 is text in, JSON out, with no workspace to read — and its prompt
# carries the PR description and diff, which are untrusted. Nothing to gain
# from a tool, everything to lose.
NO_TOOLS = ()

# A verify agent greps a whole repo; 30 minutes matches the streamIdleTimeoutMs
# the SDK backend runs with.
DEFAULT_TIMEOUT_SECONDS = 1800

# Per-agent ceiling, not a target: a review costs cents, so anything near this
# is a loop that stopped making progress. Guards the account, not the budget.
DEFAULT_MAX_BUDGET_USD = 5.0

# claims.py sends a self-contained prompt — everything the model needs is in
# the text. Saying so up front avoids a wasted turn spent reaching for Read in
# a directory that holds nothing to read.
NO_TOOLS_HINT = ("Answer directly from the information given above. "
                 "Do not use tools.")

# A run that died inside the API rather than on its own terms. Seen for real:
# four agents launched together all came back at 11s with "Not logged in ·
# Please run /login" — terminal_reason api_error, one turn, zero cost — while
# the CLI was logged in the whole time and a ping right after succeeded. A
# blip like that took down the entire review, because unlike the DeepSeek
# backend nothing here tried twice.
RETRY_TERMINAL_REASONS = ("api_error",)
DEFAULT_RETRIES = 3


def available() -> bool:
    """True if the claude binary is on PATH."""
    return shutil.which(BINARY) is not None


def version() -> str:
    """`claude --version` output, or "" if it cannot be run."""
    try:
        proc = subprocess.run([BINARY, "--version"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def build_argv(*, model: str, tools: tuple = NO_TOOLS,
               system: str | None = None,
               max_budget_usd: float | None = DEFAULT_MAX_BUDGET_USD) -> list[str]:
    """The full command line for one headless turn.

    `tools` defaults to none at all, so a new call site has to ask for reach
    rather than inherit it.

    Tools go in as one comma-separated argument rather than as the variadic
    space-separated form: variadic options swallow whatever follows them, and
    the list here is built next to other flags.
    """
    argv = [BINARY, "-p",
            "--output-format", "json",
            "--model", model,
            # Nothing inside the reviewed repo gets to configure its reviewer.
            "--safe-mode",
            "--strict-mcp-config",
            "--setting-sources", "user"]
    # --tools is the real boundary: it decides which built-in tools exist at
    # all, and "" removes every one of them. --allowedTools only says which of
    # the surviving tools may run without stopping to ask, so on its own it
    # would leave Bash present and merely unapproved — and a user settings rule
    # could approve it. Both are set from the same list so the two can never
    # disagree about what this run may touch.
    argv += ["--tools", ",".join(tools)]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    if system:
        argv += ["--append-system-prompt", system]
    if max_budget_usd:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


def run(prompt: str, *, model: str = DEFAULT_MODEL, cwd: Path | None = None,
        tools: tuple = NO_TOOLS, system: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_budget_usd: float | None = DEFAULT_MAX_BUDGET_USD,
        meta_path: Path | None = None, retries: int = DEFAULT_RETRIES,
        _run=subprocess.run, _sleep=time.sleep) -> str:
    """Run one headless turn and return the agent's final text.

    The prompt goes over stdin, never argv: a claims prompt carries the PR
    body, the diff digest and every claim as JSON, and on a large PR that
    overruns ARG_MAX long before it overruns the context window.

    `meta_path` receives the run envelope (cost, session id, permission
    denials) minus the reply itself — written before the result is judged, so
    a failed agent still leaves something to read in sessions/.

    A run that failed inside the API is retried with the same backoff the
    DeepSeek backend uses. Everything else — a bad model id, a missing binary,
    an unparseable reply — fails on the first attempt, because trying it again
    only spends the review's time to reach the same answer.
    """
    argv = build_argv(model=model, tools=tools, system=system,
                      max_budget_usd=max_budget_usd)
    for attempt in range(1, max(1, retries) + 1):
        try:
            return _attempt(argv, prompt, cwd=cwd, timeout=timeout,
                            meta_path=meta_path, _run=_run)
        except _Transient as e:
            if attempt == max(1, retries):
                raise RuntimeError(f"{e} (after {attempt} attempts)") from e
            _sleep(2 * attempt)
    raise AssertionError("unreachable")


class _Transient(RuntimeError):
    """A failure worth trying again — raised only by _attempt."""


def _attempt(argv: list[str], prompt: str, *, cwd, timeout, meta_path,
             _run) -> str:
    try:
        proc = _run(argv, input=prompt, cwd=None if cwd is None else str(cwd),
                    capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        # The PATH is part of the error because the usual cause is not a
        # missing install: it is a launcher (launchd, cron, systemd) handing
        # the process a PATH that omits the per-user bin dir, so the same
        # review works by hand and fails on a schedule.
        raise RuntimeError(
            f"{BINARY} CLI not found on PATH — install Claude Code "
            f"(https://claude.com/claude-code), or set HARNESS_PROVIDER=deepseek. "
            f"PATH was: {os.environ.get('PATH', '(unset)')}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{BINARY} timed out after {timeout}s") from e
    except OSError as e:
        raise RuntimeError(f"could not run {BINARY}: {e}") from e

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        raise RuntimeError(
            f"{BINARY} exited {proc.returncode} with no output"
            + (f": {stderr[:300]}" if stderr else ""))
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{BINARY} returned non-JSON output ({e}): {stdout[:300]}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{BINARY} returned an unexpected payload shape")

    if meta_path is not None:
        _write_meta(meta_path, data)

    result = data.get("result")
    if data.get("is_error") or data.get("subtype") != "success":
        detail = result if isinstance(result, str) and result else stderr
        # subtype stays "success" for a run that died in the API, so the
        # reason the run ended is what has to be reported and classified —
        # not the subtype, which would read as a contradiction in the log.
        reason = data.get("terminal_reason") or data.get("subtype") or "error"
        message = (f"{BINARY} failed ({reason}): "
                   f"{(detail or 'no detail')[:300]}")
        if reason in RETRY_TERMINAL_REASONS or data.get("api_error_status"):
            raise _Transient(message)
        raise RuntimeError(message)
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError(f"{BINARY} returned an empty result")
    return result


def _write_meta(path: Path, data: dict) -> None:
    """Save the run envelope next to the other session artefacts. Never fatal."""
    meta = {k: v for k, v in data.items() if k != "result"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2))
    except (OSError, TypeError, ValueError):
        pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> str | None:
    """The first complete JSON object in `text`, or None if there is none.

    The agent is told to WRITE its part file and normally does. When it answers
    with the JSON inline instead, the part file is missing and read_part()
    fails that whole axis — a docs check lost to a formatting slip. Recovering
    the object from the reply is cheaper and more honest than re-running it.
    """
    if not text:
        return None
    fence = _FENCE.search(text)
    candidate = fence.group(1).strip() if fence else text
    start = candidate.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = candidate[start:i + 1]
                try:
                    json.loads(blob)
                except json.JSONDecodeError:
                    return None
                return blob
    return None


def chat(messages: list[dict], *, model: str, api_key: str = "",
         base_url: str = "", max_tokens: int | None = None,
         retries: int = 3, **_) -> str:
    """`src.llm.chat`-shaped adapter, so claims.py can take this backend as-is.

    api_key, base_url, max_tokens and retries are accepted and ignored: the CLI
    carries its own credentials, its own output cap, and its own retry on
    overload. Keeping the signature identical is the whole point — it is what
    lets extract_claims() take either backend through the same `chat` argument.

    Runs with no tools at all. The prompt carries the PR description and diff,
    which are untrusted, and unlike phase 3 this call has no sandboxed
    workspace confining it — its cwd is wherever the review was started.
    """
    system = "\n\n".join(m["content"] for m in messages
                         if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages
                       if m.get("role") != "system")
    return run(user, model=model,
               system=f"{system}\n\n{NO_TOOLS_HINT}".strip())
