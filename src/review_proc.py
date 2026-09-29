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


def pid_alive(pid: int) -> bool:
    """True if pid corresponds to an existing OS process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    return pid_alive(pid)


def _read_last_log_line(path: Path, max_bytes: int = 4096) -> str:
    """Read the last non-empty line of a log file without reading the whole file."""
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        if size == 0:
            return ""
        with open(path, "r", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            lines = [l.strip() for l in f.readlines() if l.strip()]
            if lines:
                return lines[-1]
    except OSError:
        pass
    return ""


def find_process_children(parent_pid: int) -> list[int]:
    """Recursively find all child PIDs of parent_pid."""
    children: list[int] = []
    try:
        proc = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    cpid = int(line)
                    children.extend(find_process_children(cpid))
                    children.append(cpid)
            return children
    except (FileNotFoundError, OSError):
        pass

    try:
        proc = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(parent_pid)],
            capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    cpid = int(line)
                    children.extend(find_process_children(cpid))
                    children.append(cpid)
    except (FileNotFoundError, OSError):
        pass
    return children


def stop_process_by_pid(pid: int, timeout: float = 2.0) -> bool:
    """Gracefully terminate a process and its children with SIGTERM, falling back to SIGKILL."""
    import signal
    import time

    if not pid_alive(pid):
        return True

    children = find_process_children(pid)
    all_pids = children + [pid]

    for p in all_pids:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(pid_alive(p) for p in all_pids):
            return True
        time.sleep(0.05)

    for p in all_pids:
        if pid_alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    time.sleep(0.05)
    return not pid_alive(pid)


def format_elapsed(seconds: int | None) -> str:
    """Format seconds into human-readable elapsed duration (e.g., 2m 14s)."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    rem_sec = seconds % 60
    if minutes < 60:
        return f"{minutes}m {rem_sec:02d}s"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours}h {rem_min:02d}m"


def list_running_processes(session_root: Path,
                           autoreview_lock: Path | None = None) -> list[dict]:
    """Find all running review processes and background autoreview daemon."""
    from datetime import datetime
    import time

    results = []
    # 1. Running reviews
    if session_root.exists():
        for lock in sorted(session_root.glob("*/*/pr-*/review.lock")):
            try:
                meta = json.loads(lock.read_text())
                pid = int(meta.get("pid", 0))
                started_at = meta.get("started_at", "")
            except (ValueError, OSError, json.JSONDecodeError, AttributeError):
                continue
            if not pid_alive(pid):
                continue

            session_dir = lock.parent
            pr_dir_name = session_dir.name
            repo_name = session_dir.parent.name
            owner_name = session_dir.parent.parent.name
            pr_num = int(pr_dir_name.split("-")[1]) if "-" in pr_dir_name else 0

            elapsed = None
            if started_at:
                try:
                    st = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S")
                    elapsed = int((datetime.now() - st).total_seconds())
                except ValueError:
                    pass

            log_path = session_dir / "review.log"
            last_log = _read_last_log_line(log_path)

            results.append({
                "pid": pid,
                "type": "review",
                "target": f"{owner_name}/{repo_name}#{pr_num}",
                "owner": owner_name,
                "repo": repo_name,
                "pr": pr_num,
                "started_at": started_at,
                "elapsed_seconds": elapsed,
                "log_path": log_path,
                "last_log": last_log,
                "lock_path": lock,
            })

    # 2. Autoreview daemon
    ar_lock = autoreview_lock or Path("autoreview.lock")
    if ar_lock.exists():
        try:
            pid = int(ar_lock.read_text().strip())
        except (ValueError, OSError):
            pid = 0
        if pid_alive(pid):
            try:
                mtime = ar_lock.stat().st_mtime
                elapsed = int(time.time() - mtime)
                started_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
            except OSError:
                elapsed = None
                started_at = ""
            log_path = ar_lock.parent / "autoreview.log"
            last_log = _read_last_log_line(log_path)
            results.append({
                "pid": pid,
                "type": "autoreview",
                "target": "daemon",
                "owner": None,
                "repo": None,
                "pr": None,
                "started_at": started_at,
                "elapsed_seconds": elapsed,
                "log_path": log_path,
                "last_log": last_log,
                "lock_path": ar_lock,
            })

    results.sort(key=lambda r: (r["type"], str(r["target"])))
    return results


def stop_review(session_root: Path, owner: str, repo: str, pr: int) -> dict:
    """Stop the review process for a given PR and remove the lock."""
    lock = session_root / owner / repo / f"pr-{pr}" / "review.lock"
    if not lock.exists():
        return {"ok": False, "message": f"No review running for {owner}/{repo}#{pr}"}
    try:
        meta = json.loads(lock.read_text())
        pid = int(meta.get("pid", 0))
    except (ValueError, OSError, json.JSONDecodeError, AttributeError):
        pid = 0

    if pid <= 0 or not pid_alive(pid):
        try:
            lock.unlink()
        except OSError:
            pass
        return {
            "ok": True,
            "stale": True,
            "message": f"Cleaned up stale review lock for {owner}/{repo}#{pr} (process was not running)",
        }

    log_path = session_root / owner / repo / f"pr-{pr}" / "review.log"
    try:
        with open(log_path, "a") as f:
            f.write("\n[harness] review stopped by user\n")
    except OSError:
        pass

    stop_process_by_pid(pid)
    try:
        if lock.exists():
            lock.unlink()
    except OSError:
        pass

    return {
        "ok": True,
        "pid": pid,
        "message": f"Stopped review for {owner}/{repo}#{pr} (PID {pid})",
    }


def stop_autoreview(lock_path: Path = Path("autoreview.lock")) -> dict:
    """Stop the running autoreview process and remove its lock."""
    if not lock_path.exists():
        return {"ok": False, "message": "autoreview is not running"}
    try:
        pid = int(lock_path.read_text().strip())
    except (ValueError, OSError):
        pid = 0

    if pid <= 0 or not pid_alive(pid):
        try:
            lock_path.unlink()
        except OSError:
            pass
        return {
            "ok": True,
            "stale": True,
            "message": "Cleaned up stale autoreview.lock (process was not running)",
        }

    log_path = lock_path.parent / "autoreview.log"
    try:
        with open(log_path, "a") as f:
            f.write("\n[autoreview] stopped by user\n")
    except OSError:
        pass

    stop_process_by_pid(pid)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass

    return {
        "ok": True,
        "pid": pid,
        "message": f"Stopped autoreview (PID {pid})",
    }


def stop_by_pid(session_root: Path, pid: int,
                autoreview_lock: Path = Path("autoreview.lock")) -> dict:
    """Stop a process by its PID, automatically detecting PR reviews or autoreview."""
    if session_root.exists():
        for lock in session_root.glob("*/*/pr-*/review.lock"):
            try:
                meta = json.loads(lock.read_text())
                if int(meta.get("pid", 0)) == pid:
                    pr_dir_name = lock.parent.name
                    repo_name = lock.parent.parent.name
                    owner_name = lock.parent.parent.parent.name
                    pr_num = int(pr_dir_name.split("-")[1])
                    return stop_review(session_root, owner_name, repo_name, pr_num)
            except (ValueError, OSError, json.JSONDecodeError, AttributeError):
                continue

    if autoreview_lock.exists():
        try:
            if int(autoreview_lock.read_text().strip()) == pid:
                return stop_autoreview(autoreview_lock)
        except (ValueError, OSError):
            pass

    if not pid_alive(pid):
        return {"ok": False, "message": f"Process {pid} is not running"}
    stop_process_by_pid(pid)
    return {"ok": True, "pid": pid, "message": f"Stopped process {pid}"}


def stop_all_processes(session_root: Path,
                       autoreview_lock: Path = Path("autoreview.lock")) -> list[dict]:
    """Stop all active review processes and autoreview daemon."""
    running = list_running_processes(session_root, autoreview_lock)
    results = []
    for p in running:
        if p["type"] == "review":
            res = stop_review(session_root, p["owner"], p["repo"], p["pr"])
        elif p["type"] == "autoreview":
            res = stop_autoreview(p["lock_path"])
        else:
            res = stop_by_pid(session_root, p["pid"], autoreview_lock)
        results.append(res)
    return results


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
               timeout_seconds: float | None = None,
               max_agents: int | None = None) -> int:
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
    if max_agents is not None:
        # The child fans out into several agents; the cap is global across all
        # concurrent reviews, so it has to travel with the process.
        env["HARNESS_MAX_AGENTS"] = str(max_agents)

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
