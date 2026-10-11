# tests/test_verify_resume.py — an interrupted review carries on, it does not restart
import json
import os
import subprocess
import time

import pytest

from src import checkpoint, review_budget
from src.verify import (AgentOutputError, _execute, clear_stale_git_locks,
                        plan_tasks, run_verify, setup_workspace)


@pytest.fixture(autouse=True)
def isolated_agent_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SLOT_ROOT", str(tmp_path / "slots"))
    monkeypatch.delenv(review_budget.DEADLINE_ENV, raising=False)


SNAP = {"title": "T", "body": "B", "head_sha": "h1", "threads": [],
        "files": [{"filename": "a.py", "status": "modified", "additions": 1,
                   "deletions": 0, "patch": "@@ -1 +1 @@\n+x"}]}
CLAIMS = [{"id": "C1", "text": "x", "category": "feature", "source": "stated"}]
PAYLOADS = {
    "claims": {"claims": [{"id": "C1", "status": "PASS", "evidence": [], "note": ""}],
               "unresolved_questions": []},
    "docs": {"docs": [{"path": "d.md", "status": "STALE", "what": "old"}],
             "unresolved_questions": []},
    "impact": {"impact": [], "threads": [], "unresolved_questions": []},
    "code": {"code": [], "unresolved_questions": []},
}


def _dirs(tmp_path):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    sd.mkdir()
    return ws, sd


def _runner(calls, fail=(), silent=()):
    """Records each run. `fail` raises; `silent` answers without writing a file."""
    def runner(cfg, workspace, session_dir, task):
        calls.append(task["name"])
        if task["name"] in fail:
            raise RuntimeError("model exploded")
        if task["name"] not in silent:
            (workspace / task["out"]).write_text(
                json.dumps(PAYLOADS[task["name"]]))
        return "log"
    return runner


def _verify(tmp_path, runner, snap=SNAP, **kw):
    ws, sd = _dirs(tmp_path) if not (tmp_path / "ws").exists() else (
        tmp_path / "ws", tmp_path / "sd")
    return run_verify({"model": "m"}, ws, sd, snap, CLAIMS, runner=runner, **kw)


def test_a_second_attempt_reruns_only_the_agents_that_did_not_finish(tmp_path, capsys):
    first = []
    findings = _verify(tmp_path, _runner(first, fail=("docs",)))
    assert sorted(first) == ["claims", "code", "docs", "impact"]
    assert findings["docs"] == []  # the gap is reported, as before

    second = []
    findings = _verify(tmp_path, _runner(second))
    assert second == ["docs"]  # claims, impact and code were kept
    assert findings["docs"][0]["status"] == "STALE"
    assert findings["claims"][0]["id"] == "C1"
    assert "kept from an earlier attempt" in capsys.readouterr().out


def test_a_moved_head_reruns_every_agent(tmp_path):
    _verify(tmp_path, _runner([]))
    again = []
    _verify(tmp_path, _runner(again), snap={**SNAP, "head_sha": "h2"})
    assert sorted(again) == ["claims", "code", "docs", "impact"]


def test_a_changed_diff_reruns_the_code_agent(tmp_path):
    _verify(tmp_path, _runner([]))
    edited = {**SNAP, "files": [{**SNAP["files"][0], "patch": "@@ -1 +1 @@\n+y"}]}
    again = []
    _verify(tmp_path, _runner(again), snap=edited)
    assert "code" in again


def test_cleared_checkpoints_mean_a_full_rerun(tmp_path):
    _verify(tmp_path, _runner([]))
    checkpoint.clear(tmp_path / "sd")
    again = []
    _verify(tmp_path, _runner(again))
    assert sorted(again) == ["claims", "code", "docs", "impact"]


# --- the review deadline ------------------------------------------------------

def _past_deadline(monkeypatch):
    monkeypatch.setenv(review_budget.DEADLINE_ENV, str(time.time() - 10))


def test_out_of_time_with_nothing_done_defers_instead_of_failing(tmp_path, monkeypatch):
    _past_deadline(monkeypatch)
    calls = []
    with pytest.raises(review_budget.ReviewIncomplete, match="4 of 4 agents"):
        _verify(tmp_path, _runner(calls), may_defer=True)
    assert calls == []  # no agent was started with no time to finish


def test_out_of_time_on_the_last_attempt_ships_the_gap(tmp_path, monkeypatch):
    # claims/docs/impact finished earlier; only the code agent is missing.
    _verify(tmp_path, _runner([]))
    (tmp_path / "sd" / checkpoint.DIR / "code.json").unlink()
    _past_deadline(monkeypatch)
    calls = []
    findings = _verify(tmp_path, _runner(calls), may_defer=False)
    assert calls == []
    assert findings["claims"] and findings["docs"]  # finished work is kept
    assert any("Review gap" in q and "code" in q and "deadline" in q
               for q in findings["unresolved_questions"])


def test_out_of_time_with_one_agent_left_defers(tmp_path, monkeypatch):
    _verify(tmp_path, _runner([]))
    (tmp_path / "sd" / checkpoint.DIR / "code.json").unlink()
    _past_deadline(monkeypatch)
    with pytest.raises(review_budget.ReviewIncomplete, match="1 of 4.*code"):
        _verify(tmp_path, _runner([]), may_defer=True)


def test_agents_get_what_is_left_of_the_review_as_their_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(review_budget.DEADLINE_ENV, str(time.time() + 600))
    seen = {}

    def runner(cfg, workspace, session_dir, task):
        seen[task["name"]] = task.get("timeout")
        (workspace / task["out"]).write_text(json.dumps(PAYLOADS[task["name"]]))
        return "log"

    _verify(tmp_path, runner)
    assert all(0 < t <= 600 for t in seen.values())


def test_no_deadline_means_no_timeout_is_forced_on_the_agent(tmp_path):
    seen = {}

    def runner(cfg, workspace, session_dir, task):
        seen[task["name"]] = "timeout" in task
        (workspace / task["out"]).write_text(json.dumps(PAYLOADS[task["name"]]))
        return "log"

    _verify(tmp_path, runner)
    assert not any(seen.values())


def test_an_agent_cut_short_by_the_deadline_counts_as_out_of_time(tmp_path, monkeypatch):
    monkeypatch.setenv(review_budget.DEADLINE_ENV, str(time.time() + 61))

    def runner(cfg, workspace, session_dir, task):
        # The CLI wrapper's own timeout fires exactly when the review's runs out.
        monkeypatch.setenv(review_budget.DEADLINE_ENV, str(time.time() - 1))
        raise RuntimeError("agy timed out after 61s")

    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _, part, err = _execute({"model": "m"}, ws, sd, task, runner)
    assert part is None
    assert review_budget.is_deadline_error(err)
    assert "timed out" in err


def test_a_plain_failure_is_not_mistaken_for_running_out_of_time(tmp_path, monkeypatch):
    monkeypatch.setenv(review_budget.DEADLINE_ENV, str(time.time() + 3000))
    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _, _, err = _execute({"model": "m"}, ws, sd, task, _runner([], fail=("claims",)))
    assert err == "model exploded"
    assert not review_budget.is_deadline_error(err)


# --- an unusable answer gets one more try -------------------------------------

def test_an_unusable_answer_is_retried_once(tmp_path):
    calls = []

    def runner(cfg, workspace, session_dir, task):
        calls.append(task["name"])
        if len(calls) == 1:
            return "I forgot to write the file"  # → AgentOutputError
        (workspace / task["out"]).write_text(json.dumps(PAYLOADS[task["name"]]))
        return "log"

    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _, part, err = _execute({"model": "m"}, ws, sd, task, runner)
    assert err is None and part["claims"]
    assert calls == ["claims", "claims"]


def test_a_stale_malformed_file_is_cleared_before_the_retry(tmp_path):
    seen = []

    def runner(cfg, workspace, session_dir, task):
        seen.append((workspace / task["out"]).exists())
        if len(seen) == 1:
            (workspace / task["out"]).write_text("{broken")
        else:
            (workspace / task["out"]).write_text(json.dumps(PAYLOADS["claims"]))
        return "log"

    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    (ws / task["out"]).parent.mkdir(parents=True, exist_ok=True)
    _, part, err = _execute({"model": "m"}, ws, sd, task, runner)
    assert seen == [False, False] and err is None


def test_an_answer_that_stays_unusable_fails_after_the_second_try(tmp_path):
    calls = []
    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _, part, err = _execute({"model": "m"}, ws, sd, task,
                            _runner(calls, silent=("claims",)))
    assert part is None and "does not exist" in err
    assert calls == ["claims", "claims"]


def test_a_crash_is_not_retried(tmp_path):
    calls = []
    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _execute({"model": "m"}, ws, sd, task, _runner(calls, fail=("claims",)))
    assert calls == ["claims"]


def test_a_runner_raising_output_error_is_also_retried(tmp_path):
    calls = []

    def runner(cfg, workspace, session_dir, task):
        calls.append(1)
        raise AgentOutputError("agy returned no complete JSON object")

    ws, sd = _dirs(tmp_path)
    task = plan_tasks(SNAP, CLAIMS, [])[0]
    _, _, err = _execute({"model": "m"}, ws, sd, task, runner)
    assert len(calls) == 2 and "no complete JSON" in err


# --- stale git locks ------------------------------------------------------------

def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_old_git_locks_are_removed_and_young_ones_kept(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    stale = [git / "index.lock", git / "shallow.lock",
             git / "refs" / "heads" / "pr-1.lock"]
    for lock in stale:
        lock.write_text("")
        _age(lock, 600)
    young = git / "HEAD.lock"
    young.write_text("")

    removed = clear_stale_git_locks(tmp_path)

    assert sorted(removed) == ["index.lock", "refs/heads/pr-1.lock", "shallow.lock"]
    assert not any(p.exists() for p in stale)
    assert young.exists()  # a git still running may hold this one


def test_lock_cleanup_ignores_symlinks_and_a_missing_workspace(tmp_path):
    assert clear_stale_git_locks(tmp_path / "nope") == []
    git = tmp_path / ".git"
    git.mkdir()
    target = tmp_path / "precious"
    target.write_text("keep")
    link = git / "index.lock"
    link.symlink_to(target)
    _age(target, 600)
    assert clear_stale_git_locks(tmp_path) == []
    assert target.exists()


def _origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    (origin / "app.py").write_text("base\n")
    run("git", "add", ".")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    run("git", "checkout", "-q", "-b", "pull/7/head")
    (origin / "app.py").write_text("feature\n")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "feat")
    run("git", "checkout", "-q", "main")
    return origin


def test_a_stale_index_lock_no_longer_blocks_the_next_review(tmp_path, capsys):
    """erp#1188 failed 6 times, erp#1192 on every retry: a killed git left
    .git/index.lock and `git checkout` refused to run, whatever the time limit."""
    origin, ws = _origin(tmp_path), tmp_path / "ws"
    setup_workspace("demo", "app", 7, ws, remote_url=str(origin))
    lock = ws / ".git" / "index.lock"
    lock.write_text("")
    _age(lock, 3600)

    setup_workspace("demo", "app", 7, ws, remote_url=str(origin))

    assert (ws / "app.py").read_text() == "feature\n"
    assert not lock.exists()
    assert "removed stale git lock" in capsys.readouterr().out
