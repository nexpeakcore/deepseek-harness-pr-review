# tests/test_retry_state.py
from src import retry_state as rs


def test_first_failure_is_retried_at_once(tmp_path):
    rs.record_failure(tmp_path, "abc", now=1000)
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=1000) == 0


def test_backoff_doubles_from_the_second_failure(tmp_path):
    for _ in range(2):
        rs.record_failure(tmp_path, "abc", now=1000)
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=1000) == 4 * 60
    rs.record_failure(tmp_path, "abc", now=1000)
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=1000) == 8 * 60
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=1000 + 8 * 60) == 0


def test_backoff_is_capped(tmp_path):
    for _ in range(12):
        rs.record_failure(tmp_path, "abc", now=0)
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=0) == \
        rs.MAX_BACKOFF_MINUTES * 60


def test_a_new_head_starts_over(tmp_path):
    for _ in range(3):
        rs.record_failure(tmp_path, "old", now=1000)
    assert rs.backoff_remaining(tmp_path, "new", 2, now=1000) == 0
    assert rs.failure_count(tmp_path, "new") == 0


def test_incomplete_attempts_are_counted_per_head(tmp_path):
    rs.record_incomplete(tmp_path, "abc")
    rs.record_incomplete(tmp_path, "abc")
    assert rs.incomplete_count(tmp_path, "abc") == 2
    assert rs.incomplete_count(tmp_path, "other") == 0


def test_incomplete_attempts_do_not_trigger_backoff(tmp_path):
    for _ in range(5):
        rs.record_incomplete(tmp_path, "abc")
    assert rs.backoff_remaining(tmp_path, "abc", 2, now=1000) == 0


def test_clear_and_unreadable_state(tmp_path):
    rs.record_failure(tmp_path, "abc", now=1)
    rs.clear(tmp_path)
    assert rs.failure_count(tmp_path, "abc") == 0
    rs.clear(tmp_path)  # nothing to clear is fine
    (tmp_path / rs.STATE_FILE).write_text("{not json")
    assert rs.failure_count(tmp_path, "abc") == 0
    (tmp_path / rs.STATE_FILE).write_text('{"head_sha": "abc", "failures": "x"}')
    assert rs.failure_count(tmp_path, "abc") == 0
