"""How many times a PR's review has failed or run out of time at one head.

Two readers want this. The poller backs off a PR that keeps failing, instead of
re-running it (snapshot, claims and all) every two minutes forever. The review
process counts the attempts that ran out of time, so the last one ships what it
has instead of deferring again.

The state belongs to one head commit; a push starts it over.
"""
import json
import time
from pathlib import Path

STATE_FILE = "retry-state.json"

# Attempts a review may spend out of time before it must ship a partial report.
MAX_INCOMPLETE_ATTEMPTS = 3

# The first failure is retried at once (most are transient); from the second,
# the wait doubles per failure up to this cap.
MAX_BACKOFF_MINUTES = 60


def _load(session_dir: Path, head_sha: str) -> dict:
    fresh = {"head_sha": head_sha, "failures": 0, "incomplete": 0,
             "last_failure_at": 0.0}
    try:
        data = json.loads((session_dir / STATE_FILE).read_text())
    except (OSError, ValueError):
        return fresh
    if not isinstance(data, dict) or data.get("head_sha") != head_sha:
        return fresh
    for key in ("failures", "incomplete"):
        if not isinstance(data.get(key), int):
            data[key] = 0
    if not isinstance(data.get("last_failure_at"), (int, float)):
        data["last_failure_at"] = 0.0
    return data


def _save(session_dir: Path, state: dict) -> None:
    try:
        (session_dir / STATE_FILE).write_text(json.dumps(state))
    except OSError:
        pass  # bookkeeping must never fail a review


def record_failure(session_dir: Path, head_sha: str,
                   now: float | None = None) -> None:
    state = _load(session_dir, head_sha)
    state["failures"] += 1
    state["last_failure_at"] = time.time() if now is None else now
    _save(session_dir, state)


def record_incomplete(session_dir: Path, head_sha: str) -> None:
    state = _load(session_dir, head_sha)
    state["incomplete"] += 1
    _save(session_dir, state)


def incomplete_count(session_dir: Path, head_sha: str) -> int:
    return _load(session_dir, head_sha)["incomplete"]


def failure_count(session_dir: Path, head_sha: str) -> int:
    return _load(session_dir, head_sha)["failures"]


def backoff_remaining(session_dir: Path, head_sha: str, base_minutes: float,
                      now: float | None = None) -> float:
    """Seconds until the PR may be retried; 0 when it may be retried now."""
    state = _load(session_dir, head_sha)
    failures = state["failures"]
    if failures < 2:
        return 0.0
    wait = min(base_minutes * 2 ** (failures - 1), MAX_BACKOFF_MINUTES) * 60
    now = time.time() if now is None else now
    return max(0.0, state["last_failure_at"] + wait - now)


def clear(session_dir: Path) -> None:
    (session_dir / STATE_FILE).unlink(missing_ok=True)
