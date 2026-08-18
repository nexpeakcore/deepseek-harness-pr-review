import json
import subprocess

import pytest

from src.verify import (build_claims_prompt, build_docs_prompt,
                        build_impact_prompt, parse_findings, setup_workspace)


def test_setup_workspace_clones_and_checks_out(tmp_path):
    # Create a local "remote" repo with a pull/7/head branch
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "app.py").write_text("print('base')\n")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=origin, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "pull/7/head"], cwd=origin, check=True)
    (origin / "app.py").write_text("print('feature')\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qam", "feat"], cwd=origin, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)

    ws = tmp_path / "ws"
    setup_workspace("demo", "app", 7, ws, remote_url=str(origin))

    assert (ws / "app.py").read_text() == "print('feature')\n"


def test_setup_workspace_rerun_existing_checkout(tmp_path):
    # Workspace đã tồn tại + branch pr-7 đang checkout → re-review phải thành công
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "app.py").write_text("print('base')\n")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=origin, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "pull/7/head"], cwd=origin, check=True)
    (origin / "app.py").write_text("print('feature')\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qam", "feat"], cwd=origin, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)

    ws = tmp_path / "ws"
    setup_workspace("demo", "app", 7, ws, remote_url=str(origin))  # lần 1
    assert (ws / "app.py").read_text() == "print('feature')\n"

    # push thêm commit mới lên nhánh PR rồi re-run
    subprocess.run(["git", "checkout", "-q", "pull/7/head"], cwd=origin, check=True)
    (origin / "app.py").write_text("print('feature v2')\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qam", "feat2"], cwd=origin, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)

    setup_workspace("demo", "app", 7, ws, remote_url=str(origin))  # re-review
    assert (ws / "app.py").read_text() == "print('feature v2')\n"


def test_parse_findings_ok(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text(json.dumps({"claims": [{"id": "C1", "status": "PASS",
                                          "evidence": ["a.py:1"], "note": ""}],
                             "docs": [], "impact": [], "threads": [],
                             "unresolved_questions": []}))
    parsed = parse_findings(f)
    assert parsed["claims"][0]["status"] == "PASS"


def test_parse_findings_invalid(tmp_path):
    f = tmp_path / "findings.json"
    f.write_text("garbage")
    with pytest.raises(RuntimeError, match="invalid findings"):
        parse_findings(f)


def test_parse_findings_missing(tmp_path):
    f = tmp_path / "findings.json"
    with pytest.raises(RuntimeError, match="does not exist"):
        parse_findings(f)


def test_claims_prompt_contains_parts():
    snapshot = {"title": "T", "body": "B", "files": [{"filename": "a.py"}],
                "threads": [{"body": "c1", "resolved": False}], "commits": []}
    claims = [{"id": "C1", "text": "x", "category": "feature", "files": [], "docs": []}]
    prompt = build_claims_prompt(snapshot, claims, "findings-claims.json")
    assert "findings-claims.json" in prompt
    assert "C1" in prompt
    assert "UNVERIFIED" in prompt
    # Each agent owns one axis and is told to stay out of the others.
    assert "FABRICATED" not in prompt
    assert "another agent covers docs" in prompt


def test_docs_prompt_carries_ranked_candidates():
    snapshot = {"title": "T", "body": "B", "files": [{"filename": "a.py"}]}
    prompt = build_docs_prompt(
        snapshot, [{"path": "docs/a.md", "score": 9, "why": "mentions a.py"}],
        "findings-docs.json")
    assert "docs/a.md — mentions a.py" in prompt
    assert "FABRICATED" in prompt
    assert "starting point, not a limit" in prompt


def test_docs_prompt_without_candidates_still_works():
    prompt = build_docs_prompt({"title": "T", "body": "", "files": []}, [],
                               "findings-docs.json")
    assert "search the repo yourself" in prompt


def test_impact_prompt_covers_threads():
    snapshot = {"title": "T", "body": "B", "files": [],
                "threads": [{"author": "r1", "body": "Missing validation",
                             "resolved": False}]}
    prompt = build_impact_prompt(snapshot, [], "findings-impact.json")
    assert "Missing validation" in prompt
    assert "BROKEN" in prompt


def test_inferred_claims_switch_the_claims_prompt():
    snapshot = {"title": "fix", "body": "", "files": [], "threads": []}
    stated = build_claims_prompt(snapshot, [{"id": "C1", "source": "stated"}], "o.json")
    assert "no usable description" not in stated

    inferred = build_claims_prompt(snapshot, [{"id": "C1", "source": "inferred"}], "o.json")
    assert "no usable description" in inferred


def test_scope_creep_check_lands_on_the_impact_agent():
    snapshot = {"title": "fix", "body": "", "files": [], "threads": []}
    inferred = build_impact_prompt(snapshot, [{"id": "C1", "source": "inferred"}], "o.json")
    assert "scope creep" in inferred

    stated = build_impact_prompt(snapshot, [{"id": "C1", "source": "stated"}], "o.json")
    assert "scope creep" not in stated


def test_mixed_claims_stay_in_stated_mode():
    snapshot = {"title": "t", "body": "b", "files": [], "threads": []}
    prompt = build_claims_prompt(
        snapshot, [{"id": "C1", "source": "inferred"}, {"id": "C2", "source": "stated"}],
        "o.json")
    assert "no usable description" not in prompt


# ---------- fan-out ----------

from src.verify import merge_findings, plan_tasks, run_verify  # noqa: E402

SNAP = {"title": "T", "body": "B", "files": [{"filename": "a.py"}], "threads": []}


def _claims(n):
    return [{"id": f"C{i}", "text": "x", "category": "feature",
             "source": "stated"} for i in range(1, n + 1)]


def test_plan_tasks_one_agent_per_axis():
    tasks = plan_tasks(SNAP, _claims(3), [])
    assert [t["name"] for t in tasks] == ["claims", "docs", "impact"]
    assert len({t["out"] for t in tasks}) == 3  # no two agents share a file


def test_plan_tasks_shards_many_claims():
    tasks = plan_tasks(SNAP, _claims(37), [])
    claim_tasks = [t for t in tasks if t["axis"] == "claims"]
    assert [t["name"] for t in claim_tasks] == ["claims-1", "claims-2", "claims-3"]
    # Every claim lands in exactly one shard, in order and without overlap.
    all_claims = _claims(37)
    for i, task in enumerate(claim_tasks):
        expected = all_claims[i * 15:(i + 1) * 15]
        assert json.dumps(expected, indent=2) in task["prompt"]
    assert sum(len(all_claims[i * 15:(i + 1) * 15])
               for i in range(len(claim_tasks))) == 37
    assert "batch 1 of 3" in claim_tasks[0]["prompt"]


def test_plan_tasks_without_claims_still_reviews_docs_and_impact():
    tasks = plan_tasks(SNAP, [], [])
    assert [t["name"] for t in tasks] == ["docs", "impact"]


def test_merge_findings_concatenates_and_dedupes_questions():
    merged = merge_findings([
        {"claims": [{"id": "C1"}], "unresolved_questions": ["q1", "q2"]},
        {"docs": [{"path": "d.md"}], "unresolved_questions": ["q2", "q3"]},
        {"impact": [{"requirement": "r"}], "threads": [{"text": "t"}]},
    ])
    assert merged["claims"] == [{"id": "C1"}]
    assert merged["docs"] == [{"path": "d.md"}]
    assert merged["threads"] == [{"text": "t"}]
    assert merged["unresolved_questions"] == ["q1", "q2", "q3"]


def _fake_runner(payloads, fail=()):
    """Runner that writes each task's part file, or raises for named tasks."""
    def runner(cfg, workspace, session_dir, task):
        if task["name"] in fail:
            raise RuntimeError("model exploded")
        (workspace / task["out"]).write_text(json.dumps(payloads[task["name"]]))
        return f"log for {task['name']}"
    return runner


PAYLOADS = {
    "claims": {"claims": [{"id": "C1", "status": "PASS", "evidence": [], "note": ""}],
               "unresolved_questions": []},
    "docs": {"docs": [{"path": "d.md", "status": "STALE", "what": "old"}],
             "unresolved_questions": ["is d.md still used?"]},
    "impact": {"impact": [{"requirement": "r", "impact": "RISK", "detail": "x"}],
               "threads": [], "unresolved_questions": []},
}


def test_run_verify_merges_all_axes(tmp_path):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS))
    assert findings["claims"][0]["id"] == "C1"
    assert findings["docs"][0]["status"] == "STALE"
    assert findings["impact"][0]["impact"] == "RISK"
    # One log per agent, so a bad axis can be traced to its own transcript.
    assert (sd / "agent-log-claims.txt").exists()
    assert (sd / "agent-log-docs.txt").exists()


def test_run_verify_degrades_loudly_when_one_axis_fails(tmp_path, capsys):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS, fail=("docs",)))
    # Claims and impact still landed — the review is not thrown away.
    assert findings["claims"] and findings["impact"]
    assert findings["docs"] == []
    assert any("Review gap" in q and "docs" in q
               for q in findings["unresolved_questions"])
    assert "agent docs failed" in capsys.readouterr().err


def test_run_verify_fails_when_every_claims_agent_fails(tmp_path):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    with pytest.raises(RuntimeError, match="every claims agent failed"):
        run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                   runner=_fake_runner(PAYLOADS, fail=("claims",)))


def test_run_verify_ignores_last_rounds_part_files(tmp_path):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    # A stale docs part from the previous review must not be read as this one's.
    (ws / "findings-docs.json").write_text(json.dumps(
        {"docs": [{"path": "old.md", "status": "WRONG", "what": "stale round"}],
         "unresolved_questions": []}))
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS, fail=("docs",)))
    assert findings["docs"] == []


def _concurrency_probe(payloads, hold=0.15):
    """Runner that records how many agents were in flight at once."""
    import threading
    import time

    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    def runner(cfg, workspace, session_dir, task):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        time.sleep(hold)
        with lock:
            state["live"] -= 1
        (workspace / task["out"]).write_text(
            json.dumps({k: payloads.get(k, []) for k in task["keys"]}))
        return "log"

    return runner, state


def test_axes_actually_run_in_parallel(tmp_path, monkeypatch):
    """The point of fan-out: the axes must overlap, not queue behind each other."""
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "4")
    monkeypatch.chdir(tmp_path)          # keep .agent-slots out of the repo
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()

    runner, state = _concurrency_probe({"unresolved_questions": []})
    tasks = plan_tasks(SNAP, _claims(20), [])
    assert len(tasks) == 4               # claims-1, claims-2, docs, impact

    run_verify({"model": "m"}, ws, sd, SNAP, _claims(20), runner=runner)
    assert state["peak"] == 4


def test_global_cap_throttles_the_fan_out(tmp_path, monkeypatch):
    """max_agents is a system-wide budget, so it must bound one review too."""
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "2")
    monkeypatch.chdir(tmp_path)
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()

    runner, state = _concurrency_probe({"unresolved_questions": []})
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(20), runner=runner)
    assert state["peak"] == 2
