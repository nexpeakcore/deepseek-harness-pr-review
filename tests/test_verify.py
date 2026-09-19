import json
import subprocess

import pytest


@pytest.fixture(autouse=True)
def isolated_agent_slots(tmp_path, monkeypatch):
    """Give every test its own agent-slot pool.

    The pool is deliberately rooted at the install directory rather than the
    CWD, so chdir does not isolate it: without this the suite draws from the
    same budget as a running review and either throttles or blocks on it.
    """
    monkeypatch.setenv("HARNESS_SLOT_ROOT", str(tmp_path / "slots"))

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


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_setup_workspace_pins_the_snapshotted_head(tmp_path):
    """Codex review on #27: a push between the snapshot and the fetch had the
    agents read a newer revision than the one the review describes."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "app.py").write_text("print('base')\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-qm", "base", cwd=origin)
    _git("checkout", "-q", "-b", "pull/7/head", cwd=origin)
    (origin / "app.py").write_text("print('snapshotted')\n")
    _git("commit", "-qam", "feat", cwd=origin)
    snapshotted = _git("rev-parse", "HEAD", cwd=origin)
    # Pushed after the snapshot was taken:
    (origin / "app.py").write_text("print('newer')\n")
    _git("commit", "-qam", "feat2", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)

    ws = tmp_path / "ws"
    setup_workspace("demo", "app", 7, ws, remote_url=str(origin), head_sha=snapshotted)
    assert (ws / "app.py").read_text() == "print('snapshotted')\n"

    with pytest.raises(RuntimeError, match="no longer fetchable"):
        setup_workspace("demo", "app", 7, ws, remote_url=str(origin),
                        head_sha="0" * 40)


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
    assert [t["name"] for t in tasks] == ["claims", "docs", "impact", "code"]
    assert len({t["out"] for t in tasks}) == 4  # no two agents share a file


def test_plan_tasks_code_axis_can_be_switched_off():
    tasks = plan_tasks(SNAP, _claims(3), [], code=False)
    assert [t["name"] for t in tasks] == ["claims", "docs", "impact"]


def test_code_task_carries_its_diff_as_an_input_file():
    snap = {**SNAP, "files": [{"filename": "a.py", "status": "modified",
                               "patch": "@@ -1 +1 @@\n-x\n+y"}]}
    task = plan_tasks(snap, [], [])[-1]
    assert task["axis"] == "code"
    diff = task["inputs"]["review-diff-code.patch"]
    assert "=== a.py" in diff
    assert "1 +y" in diff                      # new-side line number in front
    assert "review-diff-code.patch" in task["prompt"]
    assert "findings-code.json" in task["prompt"]


def test_plan_tasks_shards_code_by_files():
    snap = {**SNAP, "files": [{"filename": f"f{i}.py"} for i in range(25)]}
    names = [t["name"] for t in plan_tasks(snap, [], []) if t["axis"] == "code"]
    assert names == ["code-1", "code-2"]
    assert "batch 1 of 2" in plan_tasks(snap, [], [])[2]["prompt"]


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


def test_plan_tasks_without_claims_still_reviews_docs_impact_and_code():
    tasks = plan_tasks(SNAP, [], [])
    assert [t["name"] for t in tasks] == ["docs", "impact", "code"]


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
    "code": {"code": [
        {"file": "a.py", "line": 3, "severity": "MAJOR", "category": "correctness",
         "title": "drops the last item", "scenario": "3 items -> 2 returned",
         "evidence": ["a.py:3"]},
        {"file": "a.py", "line": 9, "severity": "BLOCKER", "category": "security",
         "title": "shell injection", "scenario": "name='; rm -rf ~'",
         "evidence": ["a.py:9"]},
        {"file": "a.py", "line": 12, "severity": "MINOR", "category": "resource",
         "title": "file left open", "scenario": "each call leaks a handle",
         "evidence": []},
    ], "unresolved_questions": []},
    "code-verify": {"verdicts": [
        {"id": "K1", "verdict": "CONFIRMED", "reason": "traced"},
        {"id": "K2", "verdict": "REJECTED", "reason": "name is validated upstream"},
    ]},
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
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()

    runner, state = _concurrency_probe({"unresolved_questions": []})
    tasks = plan_tasks(SNAP, _claims(20), [])
    assert len(tasks) == 5               # claims-1, claims-2, docs, impact, code

    run_verify({"model": "m"}, ws, sd, SNAP, _claims(20), runner=runner)
    assert state["peak"] == 4


def test_global_cap_throttles_the_fan_out(tmp_path, monkeypatch):
    """max_agents is a system-wide budget, so it must bound one review too."""
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "2")
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()

    runner, state = _concurrency_probe({"unresolved_questions": []})
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(20), runner=runner)
    assert state["peak"] == 2


def test_verify_reports_progress_per_agent(tmp_path, capsys, monkeypatch):
    """Verify takes minutes; without a line per agent the live log stays empty."""
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "4")
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()

    runner, _ = _concurrency_probe({"unresolved_questions": []}, hold=0)
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(20), runner=runner)

    out = capsys.readouterr().out
    # The backend is named too: a review that silently ran on the wrong
    # provider is otherwise indistinguishable in the live log.
    assert "5 agents on deepseek/m: claims-1, claims-2, docs, impact, code" in out
    assert "cap 4 concurrent" in out
    for name in ("claims-1", "claims-2", "docs", "impact", "code"):
        assert f"{name}: done in" in out


def test_verify_reports_a_failed_agent_in_the_log(tmp_path, capsys):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
               runner=_fake_runner(PAYLOADS, fail=("docs",)))
    assert "docs: FAILED after" in capsys.readouterr().out


# --- code axis --------------------------------------------------------------

def _dirs(tmp_path):
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    return ws, sd


def _recording(payloads, fail=()):
    """_fake_runner that also records each task's prompt by name."""
    base, seen = _fake_runner(payloads, fail=fail), {}

    def runner(cfg, workspace, session_dir, task):
        seen[task["name"]] = task["prompt"]
        return base(cfg, workspace, session_dir, task)
    return runner, seen


def test_run_verify_confirms_and_rejects_code_issues(tmp_path, capsys):
    ws, sd = _dirs(tmp_path)
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS))
    assert [i["id"] for i in findings["code"]] == ["K1", "K3"]
    assert findings["code"][0]["verified"] is True
    assert findings["code"][1]["verified"] is None      # MINOR: never checked
    assert [i["id"] for i in findings["code_rejected"]] == ["K2"]
    assert findings["code_rejected"][0]["reason"] == "name is validated upstream"
    assert findings["code_meta"] == {"shards": 1, "failed_shards": 0, "verify": "ok",
                                     "patchless_files": []}
    assert list(ws.glob(".harness-review-*/review-diff-code.patch"))
    assert "code-verify: 1 confirmed, 1 rejected" in capsys.readouterr().out


def test_a_file_github_would_not_diff_is_recorded_not_reviewed(tmp_path):
    from src.code_review import code_verdict_label

    ws, sd = _dirs(tmp_path)
    snap = {**SNAP, "files": [
        {"filename": "big.py", "status": "modified", "additions": 9000,
         "deletions": 3, "patch": ""},
        {"filename": "logo.png", "status": "modified", "additions": 0,
         "deletions": 0, "patch": ""}]}
    payloads = {**PAYLOADS, "code": {"code": [], "unresolved_questions": []}}
    findings = run_verify({"model": "m"}, ws, sd, snap, _claims(1),
                          runner=_fake_runner(payloads))
    assert findings["code_meta"]["patchless_files"] == ["big.py"]
    assert code_verdict_label(findings) == "Code: no issues found (partial)"


def test_code_verifier_only_sees_blocker_and_major(tmp_path):
    ws, sd = _dirs(tmp_path)
    runner, seen = _recording(PAYLOADS)
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert "drops the last item" in seen["code-verify"]
    assert "shell injection" in seen["code-verify"]
    assert "file left open" not in seen["code-verify"]


def test_a_dead_verifier_leaves_issues_unconfirmed_and_says_so(tmp_path):
    ws, sd = _dirs(tmp_path)
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS, fail=("code-verify",)))
    assert len(findings["code"]) == 3
    assert all(i["verified"] is None for i in findings["code"])
    assert findings["code_meta"]["verify"] == "failed"
    assert any("code-verify agent failed" in q and "2 blocker/major" in q
               for q in findings["unresolved_questions"])


def test_a_dead_code_agent_reads_not_reviewed(tmp_path):
    from src.code_review import code_verdict

    ws, sd = _dirs(tmp_path)
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS, fail=("code",)))
    assert findings["code_meta"]["failed_shards"] == 1
    assert code_verdict(findings) == "NOT_RUN"
    assert any("the code agent failed" in q for q in findings["unresolved_questions"])


def test_clean_code_skips_the_verifier(tmp_path):
    ws, sd = _dirs(tmp_path)
    payloads = {**PAYLOADS, "code": {"code": [], "unresolved_questions": []}}
    runner, seen = _recording(payloads)
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert "code-verify" not in seen
    assert findings["code"] == []
    assert findings["code_meta"]["verify"] == "skipped"


def test_a_stale_verifier_answer_is_not_reused(tmp_path):
    """Last round's verdicts must not decide this round's issues."""
    ws, sd = _dirs(tmp_path)
    (ws / "findings-code-verify.json").write_text(json.dumps(
        {"verdicts": [{"id": "K1", "verdict": "REJECTED", "reason": "old"}]}))
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS, fail=("code-verify",)))
    assert findings["code_rejected"] == []


def test_code_review_off_leaves_no_code_key(tmp_path):
    ws, sd = _dirs(tmp_path)
    runner, seen = _recording(PAYLOADS)
    findings = run_verify({"model": "m", "code_review": False}, ws, sd, SNAP,
                          _claims(1), runner=runner)
    assert "code" not in findings and "code" not in seen


def test_code_verify_is_batched_and_a_dead_batch_only_costs_its_own(tmp_path,
                                                                    monkeypatch):
    """One verifier for every shard's issues has no ceiling; batches do."""
    monkeypatch.setattr("src.verify.CODE_VERIFY_BATCH", 2)
    ws, sd = _dirs(tmp_path)
    many = [{"file": "a.py", "line": n, "severity": "MAJOR",
             "category": "correctness", "title": f"bug {n}", "scenario": "s",
             "evidence": []} for n in range(1, 6)]
    base = _fake_runner({**PAYLOADS, "code": {"code": many,
                                              "unresolved_questions": []}})
    seen = []

    def runner(cfg, workspace, session_dir, task):
        if task["axis"] != "code-verify":
            return base(cfg, workspace, session_dir, task)
        seen.append(task["name"])
        if task["name"] == "code-verify-2":
            raise RuntimeError("out of budget")
        (workspace / task["out"]).write_text(json.dumps({"verdicts": [
            {"id": i["id"], "verdict": "CONFIRMED"} for i in task["issues"]]}))
        return "log"

    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert sorted(seen) == ["code-verify-1", "code-verify-2", "code-verify-3"]
    assert {i["id"]: i["verified"] for i in findings["code"]} == {
        "K1": True, "K2": True, "K3": None, "K4": None, "K5": True}
    assert findings["code_meta"]["verify"] == "partial"
    assert any("code-verify-2 agent failed" in q and "2 blocker/major" in q
               for q in findings["unresolved_questions"])


def test_review_inputs_go_where_the_pr_cannot_plant_anything(tmp_path):
    """The workspace is the PR's tree. A symlink or a directory it commits at
    a predictable name must neither redirect the write nor stop the review."""
    ws, sd = _dirs(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("precious")
    (ws / "review-diff-code.patch").symlink_to(target)
    (ws / "review-diff-code-1.patch").mkdir()
    base, seen = _fake_runner(PAYLOADS), {}

    def runner(cfg, workspace, session_dir, task):
        if task["axis"] == "code":
            diff = next(w for w in task["prompt"].split()
                        if w.startswith(".harness-review-"))
            seen["diff"] = (workspace / diff).read_text()
        return base(cfg, workspace, session_dir, task)

    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert target.read_text() == "precious"
    assert "=== a.py" in seen["diff"]
    assert findings["code_meta"]["failed_shards"] == 0


def test_a_previous_rounds_input_directory_is_cleared(tmp_path):
    ws, sd = _dirs(tmp_path)
    for _ in range(2):
        run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                   runner=_fake_runner(PAYLOADS))
    assert len(list(ws.glob(".harness-review-*"))) == 1


def test_a_verifier_cannot_decide_another_batchs_issue(tmp_path, monkeypatch):
    """Codex review on #27: batch 1 rejecting K3 must not override batch 2,
    which was the one asked about K3 and confirmed it."""
    monkeypatch.setattr("src.verify.CODE_VERIFY_BATCH", 2)
    ws, sd = _dirs(tmp_path)
    many = [{"file": "a.py", "line": n, "severity": "MAJOR",
             "category": "correctness", "title": f"bug {n}", "scenario": "s",
             "evidence": []} for n in range(1, 5)]
    base = _fake_runner({**PAYLOADS, "code": {"code": many,
                                              "unresolved_questions": []}})

    def runner(cfg, workspace, session_dir, task):
        if task["axis"] != "code-verify":
            return base(cfg, workspace, session_dir, task)
        verdicts = [{"id": i["id"], "verdict": "CONFIRMED"} for i in task["issues"]]
        if task["name"] == "code-verify-1":
            verdicts.append({"id": "K3", "verdict": "REJECTED", "reason": "not mine"})
        (workspace / task["out"]).write_text(json.dumps({"verdicts": verdicts}))
        return "log"

    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert findings["code_rejected"] == []
    assert all(i["verified"] is True for i in findings["code"])


def test_the_verifier_reads_the_diff_and_rejects_what_predates_the_pr(tmp_path):
    """Codex review on #27: confirming a defect because it happens at the head
    is not enough — the change must have caused it."""
    ws, sd = _dirs(tmp_path)
    runner, seen = _recording(PAYLOADS)
    run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    prompt = seen["code-verify"]
    diff = next(w for w in prompt.split() if w.startswith(".harness-review-"))
    assert diff.endswith("review-diff-code.patch")
    assert "predates this PR" in prompt


def test_a_pr_directory_sharing_the_prefix_is_left_alone(tmp_path):
    """Codex review on #27: clearing by pattern deleted a directory the PR
    itself committed — code that should have been reviewed."""
    ws, sd = _dirs(tmp_path)
    theirs = ws / ".harness-review-theirs"
    theirs.mkdir()
    (theirs / "keep.py").write_text("x = 1\n")
    for _ in range(2):
        run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                   runner=_fake_runner(PAYLOADS))
    assert (theirs / "keep.py").read_text() == "x = 1\n"
    assert len(list(ws.glob(".harness-review-*"))) == 2     # theirs + this round's


def test_a_verifier_id_that_is_not_a_string_is_ignored(tmp_path):
    """Codex review on #27: an unhashable id crashed the membership test."""
    ws, sd = _dirs(tmp_path)
    base = _fake_runner(PAYLOADS)

    def runner(cfg, workspace, session_dir, task):
        if task["axis"] != "code-verify":
            return base(cfg, workspace, session_dir, task)
        (workspace / task["out"]).write_text(json.dumps({"verdicts": [
            {"id": ["K1"], "verdict": "REJECTED"},
            {"id": "K2", "verdict": "CONFIRMED"}]}))
        return "log"

    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1), runner=runner)
    assert findings["code_rejected"] == []
    assert {i["id"]: i["verified"] for i in findings["code"]} == {
        "K1": None, "K2": True, "K3": None}


def test_review_input_is_utf8_whatever_the_locale(tmp_path):
    """Found by this PR's fourth review: with no explicit encoding, a diff
    carrying a non-ASCII character crashed the review under a non-UTF-8
    locale. Run the write in a child process that has such a locale."""
    import os
    import subprocess
    import sys

    text = "đã sửa — ✓"
    # The text is spelled in ASCII escapes inside the child: passed through
    # argv, a legacy locale would decode it into surrogates before the write
    # under test ever ran (Codex review on #27).
    code = ("import sys, locale, pathlib; from src.verify import _write_input; "
            f"_write_input(pathlib.Path(sys.argv[1]), {ascii(text)}); "
            "print(locale.getencoding())")
    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "en_US.ISO8859-1",
           "LANG": "en_US.ISO8859-1", "PYTHONPATH": "."}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path / "d.patch")],
                          capture_output=True, text=True, env=env, cwd=root)
    if proc.returncode == 0 and proc.stdout.strip().lower().replace("-", "") == "utf8":
        pytest.skip("no non-UTF-8 locale available here")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "d.patch").read_bytes() == text.encode("utf-8")


def test_agent_outputs_never_touch_the_prs_own_files(tmp_path):
    """Codex review on #27: output files at the checkout root were unlinked
    and overwritten when the PR had a file of the same name, and a directory
    of that name aborted the review."""
    ws, sd = _dirs(tmp_path)
    (ws / "findings-code.json").write_text('{"theirs": true}')
    (ws / "findings-docs.json").mkdir()
    findings = run_verify({"model": "m"}, ws, sd, SNAP, _claims(1),
                          runner=_fake_runner(PAYLOADS))
    assert (ws / "findings-code.json").read_text() == '{"theirs": true}'
    assert (ws / "findings-docs.json").is_dir()
    assert findings["docs"][0]["status"] == "STALE"          # docs still landed
    assert findings["code_meta"]["failed_shards"] == 0


# --- agent backend selection ------------------------------------------------

def test_select_runner_defaults_to_the_sdk_backend():
    from src.verify import RUNNERS, select_runner

    assert select_runner({}) is RUNNERS["deepseek"]
    assert select_runner({"provider": "  Claude "}) is RUNNERS["claude"]
    assert select_runner({"provider": "codex"}) is RUNNERS["codex"]


def test_select_runner_names_the_valid_providers():
    """A typo in HARNESS_PROVIDER must not silently fall back to the default."""
    from src.verify import select_runner

    with pytest.raises(RuntimeError, match="claude, codex, deepseek"):
        select_runner({"provider": "cluade"})


def test_backend_label_reports_what_actually_ran():
    from src.verify import backend_label

    assert backend_label({"model": "deepseek-v4-flash"}) == "deepseek/deepseek-v4-flash"
    assert backend_label({"provider": "claude", "model": "opus"}) == "claude/opus"
    assert backend_label({"provider": "codex", "model": "gpt-5.5"}) == "codex/gpt-5.5"


def test_codex_runner_writes_the_validated_inline_object(tmp_path, monkeypatch):
    from src import codex_cli
    from src.verify import _run_agent_codex

    monkeypatch.setattr(codex_cli, "run",
                        lambda prompt, **kw: '{"docs": [], "unresolved_questions": []}')
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    _run_agent_codex({"codex_model": "gpt-5.5"}, ws, sd,
                     {"name": "docs", "out": "findings-docs.json", "prompt": "..."})
    assert json.loads((ws / "findings-docs.json").read_text()) == {
        "docs": [], "unresolved_questions": []}


def test_claude_runner_salvages_a_part_answered_inline(tmp_path, monkeypatch):
    """Losing a whole review axis to a formatting slip is not an acceptable trade."""
    from src import claude_cli
    from src.verify import _run_agent_claude

    monkeypatch.setattr(
        claude_cli, "run",
        lambda prompt, **kw: 'Here it is:\n```json\n{"docs": [], '
                             '"unresolved_questions": []}\n```')
    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    task = {"name": "docs", "out": "findings-docs.json", "prompt": "..."}

    _run_agent_claude({"claude_model": "sonnet"}, ws, sd, task)

    assert json.loads((ws / "findings-docs.json").read_text()) == {
        "docs": [], "unresolved_questions": []}


def test_claude_runner_keeps_the_file_the_agent_wrote(tmp_path, monkeypatch):
    """The Write tool is the normal path; salvage must never clobber it."""
    from src import claude_cli
    from src.verify import _run_agent_claude

    ws, sd = tmp_path / "ws", tmp_path / "sd"
    ws.mkdir()
    written = {"docs": [{"path": "README.md", "status": "STALE", "what": "x"}],
               "unresolved_questions": []}
    (ws / "findings-docs.json").write_text(json.dumps(written))
    monkeypatch.setattr(claude_cli, "run",
                        lambda prompt, **kw: '{"docs": [], "unresolved_questions": []}')

    _run_agent_claude({}, ws, sd, {"name": "docs", "out": "findings-docs.json",
                                   "prompt": "..."})

    assert json.loads((ws / "findings-docs.json").read_text()) == written
