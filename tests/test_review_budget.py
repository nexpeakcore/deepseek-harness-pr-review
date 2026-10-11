# tests/test_review_budget.py
from src import review_budget as rb


def test_no_deadline_in_the_environment_means_no_limit(monkeypatch):
    monkeypatch.delenv(rb.DEADLINE_ENV, raising=False)
    assert rb.deadline() is None
    assert rb.time_left() is None


def test_garbage_deadline_is_ignored(monkeypatch):
    monkeypatch.setenv(rb.DEADLINE_ENV, "soon")
    assert rb.deadline() is None


def test_time_left_counts_down_to_the_deadline(monkeypatch):
    monkeypatch.setenv(rb.DEADLINE_ENV, "1000")
    assert rb.time_left(now=400) == 600
    assert rb.time_left(now=1100) == -100


def test_soft_deadline_leaves_room_before_the_kill():
    assert rb.soft_deadline_for(1800, now=0) == 1800 - rb.SOFT_DEADLINE_GRACE_SECONDS
    # A short timeout keeps a proportional margin rather than going negative.
    assert rb.soft_deadline_for(100, now=0) == 80


def test_deadline_errors_are_recognised():
    assert rb.is_deadline_error(f"{rb.DEADLINE_PREFIX}: agent not started")
    assert not rb.is_deadline_error("model exploded")
    assert not rb.is_deadline_error(None)
