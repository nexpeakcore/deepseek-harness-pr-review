# tests/test_run_resume.py — run.py around an interrupted review
import json

import pytest

from src import checkpoint, retry_state
from src.review_budget import ReviewIncomplete
from src.review_proc import EXIT_INCOMPLETE
from src.run import main

SNAPSHOT = {"owner": "demo", "repo": "app", "pr": 7, "title": "T", "body": "B",
            "author": "a", "base": "main", "head": "x", "head_sha": "h1",
            "labels": [], "files": [], "commits": [], "threads": []}
FINDINGS = {"claims": [], "docs": [], "impact": [], "threads": [],
            "unresolved_questions": []}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """run.py with every external step replaced by a counter."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    session = tmp_path / "sessions" / "demo" / "app" / "pr-7"
    state = {"snapshot": 0, "claims": 0, "verify": [], "session": session,
             "outcome": FINDINGS}

    def build_snapshot(owner, repo, n, session_dir, gh=None):
        state["snapshot"] += 1
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshot.json").write_text(json.dumps(SNAPSHOT))
        return SNAPSHOT

    def extract_claims(snapshot, cfg, session_dir, chat=None):
        state["claims"] += 1
        (session_dir / "claims.json").write_text("[]")
        return []

    def run_verify(cfg, workspace, session_dir, snapshot, claims, **kw):
        state["verify"].append(kw)
        if isinstance(state["outcome"], Exception):
            raise state["outcome"]
        return state["outcome"]

    monkeypatch.setattr("src.snapshot.build_snapshot", build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", lambda *a, **k: None)
    monkeypatch.setattr("src.run.run_verify", run_verify)
    return state


def _seed(session):
    """What an interrupted review of head h1 leaves behind."""
    session.mkdir(parents=True, exist_ok=True)
    (session / "snapshot.json").write_text(json.dumps(SNAPSHOT))
    (session / "claims.json").write_text("[]")
    (session / checkpoint.DIR).mkdir()
    (session / checkpoint.DIR / "code-1.json").write_text("{}")


ARGS = ["demo/app", "7", "--no-post", "--skip-human"]


def test_resume_keeps_the_snapshot_claims_and_finished_agents(pipeline):
    _seed(pipeline["session"])
    assert main(ARGS + ["--force", "--resume"]) == 0
    assert pipeline["snapshot"] == 0 and pipeline["claims"] == 0
    assert len(pipeline["verify"]) == 1  # findings are still recomputed


def test_force_alone_starts_over(pipeline):
    _seed(pipeline["session"])
    assert main(ARGS + ["--force"]) == 0
    assert pipeline["snapshot"] == 1 and pipeline["claims"] == 1


def test_force_alone_discards_checkpoints_before_verifying(pipeline, monkeypatch):
    _seed(pipeline["session"])
    seen = {}
    real = pipeline["verify"]

    def spy(cfg, workspace, session_dir, snapshot, claims, **kw):
        seen["checkpoints"] = (session_dir / checkpoint.DIR).exists()
        return FINDINGS

    monkeypatch.setattr("src.run.run_verify", spy)
    main(ARGS + ["--force"])
    assert seen["checkpoints"] is False and real == []


def test_running_out_of_time_exits_incomplete_and_counts_the_attempt(pipeline):
    pipeline["outcome"] = ReviewIncomplete("out of time with 3 of 14 agents")
    session = pipeline["session"]

    assert main(ARGS + ["--force"]) == EXIT_INCOMPLETE

    assert retry_state.incomplete_count(session, "h1") == 1
    assert not (session / "findings.json").exists()
    assert not (session / "review.lock").exists()  # released for the next attempt
    assert not (session / "report.md").exists()  # no "failed" report either


def test_early_attempts_may_defer_the_last_one_may_not(pipeline):
    pipeline["outcome"] = ReviewIncomplete("x")
    session = pipeline["session"]
    seen = []
    for _ in range(retry_state.MAX_INCOMPLETE_ATTEMPTS):
        main(ARGS + ["--force", "--resume"])
        seen.append(pipeline["verify"][-1]["may_defer"])
    assert seen == [True, True, False]


def test_a_finished_review_clears_the_leftovers(pipeline):
    session = pipeline["session"]
    _seed(session)
    retry_state.record_incomplete(session, "h1")
    retry_state.record_failure(session, "h1")

    assert main(ARGS + ["--force", "--resume"]) == 0

    assert not (session / checkpoint.DIR).exists()
    assert retry_state.incomplete_count(session, "h1") == 0
    assert retry_state.failure_count(session, "h1") == 0
    assert (session / "findings.json").exists()
