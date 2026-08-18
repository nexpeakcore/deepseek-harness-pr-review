"""Run one PR review as a separate OS process.

Reviews used to run in-process, which made parallelism impossible:
`contextlib.redirect_stdout` mutates `sys.stdout` for the whole interpreter, so
two reviews running at once interleave their logs and the second one to finish
restores a `sys.stdout` that the first has already closed.

A subprocess gets its own stdout, its own module-level globals, and — because
`run.py` records `os.getpid()` in `review.lock` — a PID that actually belongs to
the review. In-process runs recorded the *web server's* PID, so a review that
died inside a live server still looked alive to `review_process_info` forever.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Exit codes run.py never returns, so callers can tell these apart from a
# review that ran and failed on its own terms.
EXIT_TIMEOUT = 124  # same convention as timeout(1)
EXIT_SPAWN_FAILED = 125


def review_lock_alive(lock: Path) -> bool:
    """True if review.lock records a PID that is still running.

    Missing, corrupt or unparseable lock, or a PID that no longer exists →
    stale. A PID owned by another user raises PermissionError from
    kill(pid, 0), which means the process IS alive.

    Shared by run.py (which reclaims stale locks on acquire) and autoreview.py
    (which must not skip a PR whose lock is stale) so the two can never
    disagree about what "a review is running" means.
    """
    try:
        pid = int(json.loads(lock.read_text()).get("pid", 0))
    except (ValueError, OSError, AttributeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_argv(owner: str, repo: str, n: int, *, force: bool = True,
               skip_human: bool = True, no_post: bool = False,
               no_ping: bool = False) -> list[str]:
    """argv for `python -m src.run` — kept separate so tests can assert on it."""
    argv = [f"{owner}/{repo}", str(n)]
    if force:
        argv.append("--force")
    if skip_human:
        argv.append("--skip-human")
    if no_post:
        argv.append("--no-post")
    if no_ping:
        argv.append("--no-ping")
    return argv


def run_review(owner: str, repo: str, n: int, *, session_root: Path,
               log_path: Path, force: bool = True, skip_human: bool = True,
               no_post: bool = False, no_ping: bool = False,
               cwd: Path | None = None,
               timeout_seconds: float | None = None) -> int:
    """Run one review in its own process. Returns run.py's exit code.

    Output (stdout + stderr) goes straight to `log_path`; nothing about the
    parent's streams is touched, so N of these can run at once.

    stdin is /dev/null: a subprocess has no terminal to prompt on. human_gate
    turns the resulting EOFError into a SKIPPED answer rather than crashing,
    so this is safe even without --skip-human.
    """
    env = dict(os.environ)
    env["DSH_SESSION_ROOT"] = str(session_root)
    env["PYTHONUNBUFFERED"] = "1"  # log is tailed live by the dashboard

    cmd = [sys.executable, "-m", "src.run",
           *build_argv(owner, repo, n, force=force, skip_human=skip_human,
                       no_post=no_post, no_ping=no_ping)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "w", buffering=1) as logf:
            proc = subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd or Path.cwd()), env=env,
                timeout=timeout_seconds)
        return proc.returncode
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child. Its review.lock is left
        # behind holding a dead PID, which _acquire_review_lock reclaims on the
        # next attempt — that is exactly the stale-lock path, not a leak.
        with open(log_path, "a") as logf:
            logf.write(f"\n[harness] review timed out after "
                       f"{timeout_seconds}s and was killed\n")
        return EXIT_TIMEOUT
    except OSError as e:
        with open(log_path, "a") as logf:
            logf.write(f"\n[harness] could not start review process: {e}\n")
        return EXIT_SPAWN_FAILED
