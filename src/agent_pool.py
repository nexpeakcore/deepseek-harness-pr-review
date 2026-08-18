"""Global cap on concurrent model agents, shared across review processes.

Fan-out multiplied the agent count. `autoreview` runs `max_parallel` reviews at
once, and each review now runs several agents in parallel, so the number of
in-flight model calls is the *product* of the two — a number neither side can
see on its own. The real ceiling is API concurrency, not CPU, so the cap has to
be global.

Nothing in one process can observe another's threads, so the cap lives on disk:
`limit` slot files, each taken with O_CREAT|O_EXCL and removed on release. A
slot whose PID is dead is reclaimed, same staleness rule as review.lock, so a
killed review never wedges the pool.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from src.review_proc import review_lock_alive

SLOT_DIR_NAME = ".agent-slots"
DEFAULT_MAX_AGENTS = 4
# Hard ceiling, same spirit as MAX_PARALLEL_CAP: not a technical limit, a guard
# against a typo in the env burning the API quota in one pass.
MAX_AGENTS_CAP = 16
# A dead-PID slot is only reclaimable once it is this old. Without the grace
# window two processes can both decide the same slot is stale, and the second
# unlink lands on the lock the first has already re-created — it would steal a
# live slot. Nothing creates a lock and dies within the window in practice.
STALE_GRACE_SECONDS = 10


def max_agents() -> int:
    """Concurrent-agent cap from HARNESS_MAX_AGENTS, clamped to 1..CAP."""
    try:
        value = int(os.environ.get("HARNESS_MAX_AGENTS", DEFAULT_MAX_AGENTS))
    except ValueError:
        return DEFAULT_MAX_AGENTS
    return max(1, min(value, MAX_AGENTS_CAP))


def default_root() -> Path:
    """Where the slot files live when no caller names a directory.

    Not the CWD: a manual `python -m src.run` started from another directory
    would then keep its own private pool and the cap would silently stop being
    global — the one property it exists for. The install directory is stable no
    matter where the process was launched from. HARNESS_SLOT_ROOT overrides it
    for setups that share one API quota across several checkouts.
    """
    override = os.environ.get("HARNESS_SLOT_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent


def slot_dir(root: Path | None = None) -> Path:
    return (root or default_root()) / SLOT_DIR_NAME


def _reclaimable(path: Path) -> bool:
    """True if this slot is held by a process that no longer exists."""
    try:
        if time.time() - path.stat().st_mtime < STALE_GRACE_SECONDS:
            return False
    except OSError:
        return False
    return not review_lock_alive(path)


def _try_take(path: Path, label: str) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "label": label,
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    return True


def acquire_slot(label: str, *, limit: int | None = None,
                 root: Path | None = None, poll_seconds: float = 0.5,
                 timeout: float | None = None,
                 sleep=time.sleep) -> Path:
    """Block until a slot is free, then return the slot file holding it."""
    limit = limit or max_agents()
    directory = slot_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + timeout

    while True:
        for i in range(limit):
            path = directory / f"slot-{i}.lock"
            if _try_take(path, label):
                return path
            if _reclaimable(path):
                try:
                    path.unlink()
                except OSError:
                    pass
                if _try_take(path, label):
                    return path
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                f"no agent slot free after {timeout}s (limit={limit})")
        sleep(poll_seconds)


def release_slot(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


@contextmanager
def agent_slot(label: str, **kwargs):
    """Hold one of the global agent slots for the duration of the block."""
    path = acquire_slot(label, **kwargs)
    try:
        yield path
    finally:
        release_slot(path)
