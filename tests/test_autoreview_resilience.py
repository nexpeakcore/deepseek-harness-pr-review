# tests/test_autoreview_resilience.py — the poller around reviews that fail or run out of time
import json
from types import SimpleNamespace

from src import retry_state
from src.autoreview import _dispatch, run_pass
from src.autoreview_config import load_config
from src.review_proc import EXIT_INCOMPLETE

PR = [{"number": 1, "head": {"sha": "aaa1111"}, "draft": False}]


def _setup(tmp_path):
    root = tmp_path / "sessions"
    (root / "sample-org" / "sample-api" / "pr-1").mkdir(parents=True)
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\ninterval_minutes: 2\n"
                        "repos:\n  sample-api: auto\n")
    return root, load_config(cfg_path), root / "sample-org" / "sample-api" / "pr-1"


def _gh(prs=PR):
    return lambda args, **kw: prs


def _dispatching(monkeypatch, code):
    calls = []
    monkeypatch.setattr("src.autoreview._dispatch",
                        lambda c, o, r, n, sha: (calls.append(sha) or code))
    return calls


def test_a_failing_pr_is_backed_off_from_its_second_failure(tmp_path, monkeypatch, capsys):
    root, cfg, session = _setup(tmp_path)
    calls = _dispatching(monkeypatch, 1)

    run_pass(cfg, root, gh=_gh())  # failure 1: retried at once
    run_pass(cfg, root, gh=_gh())  # failure 2
    assert len(calls) == 2
    capsys.readouterr()

    run_pass(cfg, root, gh=_gh())  # now it waits
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert "BACKOFF sample-org/sample-api#1" in out
    assert "failed 2 times at aaa1111" in out


def test_backoff_ends_after_the_wait(tmp_path, monkeypatch):
    root, cfg, session = _setup(tmp_path)
    calls = _dispatching(monkeypatch, 1)
    for _ in range(2):
        run_pass(cfg, root, gh=_gh())
    state = json.loads((session / retry_state.STATE_FILE).read_text())
    state["last_failure_at"] -= 10 * 60  # longer than the 4 minute wait
    (session / retry_state.STATE_FILE).write_text(json.dumps(state))

    run_pass(cfg, root, gh=_gh())
    assert len(calls) == 3


def test_a_new_head_is_not_held_back_by_the_old_heads_failures(tmp_path, monkeypatch):
    root, cfg, session = _setup(tmp_path)
    calls = _dispatching(monkeypatch, 1)
    for _ in range(3):
        run_pass(cfg, root, gh=_gh())
    pushed = [{"number": 1, "head": {"sha": "bbb2222"}, "draft": False}]
    run_pass(cfg, root, gh=_gh(pushed))
    assert calls[-1] == "bbb2222"


def test_running_out_of_time_is_resumed_not_reported_as_a_failure(tmp_path, monkeypatch, capsys):
    root, cfg, session = _setup(tmp_path)
    calls = _dispatching(monkeypatch, EXIT_INCOMPLETE)
    for _ in range(4):
        run_pass(cfg, root, gh=_gh())
    captured = capsys.readouterr()
    assert len(calls) == 4  # never backed off: it is making progress
    assert "INCOMPLETE sample-org/sample-api#1" in captured.out
    assert "FAILED" not in captured.err
    assert retry_state.failure_count(session, "aaa1111") == 0


def test_a_successful_review_records_no_failure(tmp_path, monkeypatch):
    root, cfg, session = _setup(tmp_path)
    _dispatching(monkeypatch, 0)
    run_pass(cfg, root, gh=_gh())
    assert retry_state.failure_count(session, "aaa1111") == 0


def test_a_spawn_error_counts_as_a_failure(tmp_path, monkeypatch):
    root, cfg, session = _setup(tmp_path)

    def boom(c, o, r, n, sha):
        raise OSError("cannot start")

    monkeypatch.setattr("src.autoreview._dispatch", boom)
    run_pass(cfg, root, gh=_gh())
    assert retry_state.failure_count(session, "aaa1111") == 1


# --- _dispatch: what the review process is told ---------------------------------

def _capture_run_review(monkeypatch, tmp_path):
    seen = {}

    def fake(owner, repo, n, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr("src.review_proc.run_review", fake)
    monkeypatch.setattr("src.autoreview.load_env_config",
                        lambda: SimpleNamespace(session_root=tmp_path))
    return seen


def _snapshot(tmp_path, sha):
    d = tmp_path / "o" / "r" / "pr-5"
    d.mkdir(parents=True)
    (d / "snapshot.json").write_text(json.dumps({"head_sha": sha}))


def test_dispatch_resumes_an_interrupted_review_of_the_same_head(tmp_path, monkeypatch):
    seen = _capture_run_review(monkeypatch, tmp_path)
    _snapshot(tmp_path, "abc")
    _dispatch({"review_timeout_minutes": 30}, "o", "r", 5, "abc")
    assert seen["resume"] is True
    assert seen["soft_deadline"] is True
    assert seen["timeout_seconds"] == 1800


def test_dispatch_starts_over_when_the_head_moved(tmp_path, monkeypatch):
    seen = _capture_run_review(monkeypatch, tmp_path)
    _snapshot(tmp_path, "old")
    _dispatch({}, "o", "r", 5, "new")
    assert seen["resume"] is False
    assert seen["soft_deadline"] is True


def test_dispatch_starts_fresh_with_no_snapshot(tmp_path, monkeypatch):
    seen = _capture_run_review(monkeypatch, tmp_path)
    _dispatch({}, "o", "r", 5, "abc")
    assert seen["resume"] is False


def test_dispatch_does_not_resume_from_a_corrupt_snapshot(tmp_path, monkeypatch):
    seen = _capture_run_review(monkeypatch, tmp_path)
    _snapshot(tmp_path, "abc")
    (tmp_path / "o" / "r" / "pr-5" / "snapshot.json").write_text("{nope")
    _dispatch({}, "o", "r", 5, "abc")
    assert seen["resume"] is False
