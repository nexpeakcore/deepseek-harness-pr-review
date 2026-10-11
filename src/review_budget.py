"""The time a review has, as the review process itself sees it.

The poller kills a review that runs past `review_timeout_minutes`, and a killed
review loses everything it had not written down. So the poller also hands the
child a *soft* deadline a little earlier than the kill. Past it the review
stops starting agents, cuts the running ones short and exits cleanly, with the
finished agents' results saved (see checkpoint.py) for the next attempt.

No deadline in the environment — a manual run — means no behaviour change.
"""
import os
import time

DEADLINE_ENV = "HARNESS_REVIEW_DEADLINE"  # epoch seconds, set by run_review

# Room between the soft deadline and the poller's kill for the part of a review
# that is not an agent: the report, the PR comment, releasing the lock.
SOFT_DEADLINE_GRACE_SECONDS = 120

# An agent given less than this cannot read a diff, let alone answer; starting
# it only burns a slot for a result that is thrown away.
MIN_AGENT_SECONDS = 60

DEADLINE_PREFIX = "review deadline reached"


class ReviewIncomplete(Exception):
    """The deadline passed with agents unfinished; their siblings' work is saved.

    Not a RuntimeError on purpose: run.py turns those into a failed report, and
    this is not a failure — the next attempt carries on from here.
    """


def soft_deadline_for(timeout_seconds: float, now: float | None = None) -> float:
    """Epoch time the review should wrap up by, given the poller's kill timeout."""
    now = time.time() if now is None else now
    return now + timeout_seconds - min(SOFT_DEADLINE_GRACE_SECONDS,
                                       timeout_seconds * 0.2)


def deadline() -> float | None:
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def time_left(now: float | None = None) -> float | None:
    """Seconds until the soft deadline, or None when there is none."""
    limit = deadline()
    if limit is None:
        return None
    return limit - (time.time() if now is None else now)


def is_deadline_error(message: str | None) -> bool:
    return bool(message) and message.startswith(DEADLINE_PREFIX)
