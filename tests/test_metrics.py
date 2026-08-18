# tests/test_metrics.py
import json
import os

from web import metrics

EMPTY_FINDINGS = {"claims": [], "docs": [], "impact": [], "threads": [],
                  "unresolved_questions": []}

SNAPSHOT = {"pr": 7, "title": "Add checkout", "author": "dev1",
            "base": "main", "head": "x", "files": [], "commits": [], "threads": []}


def _write_session(root, owner, repo, pr, snapshot=None, findings=None,
                   answers=None, report=None):
    d = root / owner / repo / f"pr-{pr}"
    d.mkdir(parents=True, exist_ok=True)
    if snapshot is not None:
        (d / "snapshot.json").write_text(json.dumps(snapshot))
    if findings is not None:
        (d / "findings.json").write_text(json.dumps(findings))
    if answers is not None:
        (d / "answers.json").write_text(json.dumps(answers))
    if report is not None:
        (d / "report.md").write_text(report)


def test_list_repos_empty(tmp_path):
    assert metrics.list_repos(tmp_path) == []


def test_list_repos_finds_pairs(tmp_path):
    _write_session(tmp_path, "sample-org", "sample-app", 7,
                   snapshot=SNAPSHOT, findings=EMPTY_FINDINGS)
    _write_session(tmp_path, "sample-org", "sample-api", 3,
                   snapshot=SNAPSHOT, findings=EMPTY_FINDINGS)
    assert metrics.list_repos(tmp_path) == [("sample-org", "sample-api"),
                                            ("sample-org", "sample-app")]


def test_pr_record_counts(tmp_path):
    findings = {
        "claims": [{"id": "C1", "status": "FAIL", "evidence": [], "note": ""},
                   {"id": "C2", "status": "PASS", "evidence": [], "note": ""}],
        "docs": [{"path": "a.md", "status": "WRONG", "what": ""},
                 {"path": "b.md", "status": "FABRICATED", "what": ""},
                 {"path": "c.md", "status": "MATCH", "what": ""}],
        "impact": [{"requirement": "R1", "impact": "BROKEN", "detail": ""}],
        "threads": [],
        "unresolved_questions": [],
    }
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=findings,
                   answers=[{"question": "q1", "kind": "doc", "answer": "SKIPPED"},
                            {"question": "q2", "kind": "claim", "answer": "y"}])
    rec = metrics.pr_record(tmp_path, "o", "r", 7)
    assert rec["verdict"] == "CONTRADICTED"
    assert rec["bugs"] == 2            # 1 FAIL claim + 1 BROKEN impact
    assert rec["doc_errors"] == 2      # WRONG + FABRICATED
    assert rec["open_questions"] == 1  # only SKIPPED counted
    assert rec["claims_total"] == 2
    assert rec["failed"] is False


def test_pr_record_failed_phase(tmp_path):
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS,
                   report="# Review FAILED\n\n- Lỗi: boom\n")
    rec = metrics.pr_record(tmp_path, "o", "r", 7)
    assert rec["failed"] is True


def test_pr_record_missing_files_returns_none(tmp_path):
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT)  # no findings
    assert metrics.pr_record(tmp_path, "o", "r", 7) is None


def test_pr_record_corrupt_json_skipped(tmp_path):
    d = tmp_path / "o" / "r" / "pr-7"
    d.mkdir(parents=True)
    (d / "snapshot.json").write_text("garbage")
    (d / "findings.json").write_text("garbage")
    assert metrics.pr_record(tmp_path, "o", "r", 7) is None


def test_repo_record_aggregates(tmp_path):
    findings = {
        "claims": [{"id": "C1", "status": "FAIL", "evidence": [], "note": ""}],
        "docs": [], "impact": [], "threads": [], "unresolved_questions": [],
    }
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT, findings=EMPTY_FINDINGS)
    _write_session(tmp_path, "o", "r", 8, snapshot=SNAPSHOT, findings=findings)
    rec = metrics.repo_record(tmp_path, "o", "r")
    assert rec["prs_total"] == 2
    assert rec["bugs_total"] == 1
    assert rec["doc_errors_total"] == 0
    assert rec["verdict_count"] == {"ACCURATE": 0, "PARTIAL": 0,
                                    "CONTRADICTED": 1, "NO_CLAIMS": 1,
                                    "NO_DESCRIPTION": 0, "INCONSISTENT": 0}
    assert len(rec["prs"]) == 2


def test_repo_record_missing_returns_none(tmp_path):
    assert metrics.repo_record(tmp_path, "o", "nope") is None


def test_pr_detail_merges_claims(tmp_path):
    claims = [{"id": "C1", "text": "Adds checkout", "category": "feature",
               "files": [], "docs": []}]
    findings = {
        "claims": [{"id": "C1", "status": "PASS", "evidence": ["a.py:1"],
                    "note": ""}],
        "docs": [], "impact": [], "threads": [], "unresolved_questions": [],
    }
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT, findings=findings)
    (tmp_path / "o" / "r" / "pr-7" / "claims.json").write_text(
        json.dumps(claims))
    detail = metrics.pr_detail(tmp_path, "o", "r", 7)
    assert detail["claims"][0]["text"] == "Adds checkout"
    assert detail["claims"][0]["status"] == "PASS"
    assert detail["claims"][0]["category"] == "feature"


def test_pr_record_wider_metrics(tmp_path):
    findings = {
        "claims": [{"id": "C1", "status": "FAIL", "evidence": [], "note": ""},
                   {"id": "C2", "status": "PARTIAL", "evidence": [], "note": ""},
                   {"id": "C3", "status": "PASS", "evidence": [], "note": ""}],
        "docs": [{"path": "a.md", "status": "WRONG", "what": ""},
                 {"path": "b.md", "status": "STALE", "what": ""},
                 {"path": "c.md", "status": "FABRICATED", "what": ""},
                 {"path": "d.md", "status": "MATCH", "what": ""}],
        "impact": [{"requirement": "R1", "impact": "BROKEN", "detail": ""},
                   {"requirement": "R2", "impact": "RISK", "detail": ""},
                   {"requirement": "R3", "impact": "CHANGED", "detail": ""}],
        "threads": [],
        "unresolved_questions": [],
    }
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT, findings=findings)
    rec = metrics.pr_record(tmp_path, "o", "r", 7)
    assert rec["bugs"] == 4          # FAIL + PARTIAL + BROKEN + RISK
    assert rec["doc_errors"] == 3    # WRONG + FABRICATED + STALE


def test_pr_record_rounds(tmp_path):
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)
    (tmp_path / "o" / "r" / "pr-7" / "rounds.txt").write_text("3")
    rec = metrics.pr_record(tmp_path, "o", "r", 7)
    assert rec["rounds"] == 3


def test_pr_record_rounds_fallback(tmp_path):
    # không có rounds.txt → 1
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)
    assert metrics.pr_record(tmp_path, "o", "r", 7)["rounds"] == 1


def test_pr_record_rounds_garbage(tmp_path):
    _write_session(tmp_path, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)
    (tmp_path / "o" / "r" / "pr-7" / "rounds.txt").write_text("abc")
    assert metrics.pr_record(tmp_path, "o", "r", 7)["rounds"] == 1


def test_open_prs_merge(tmp_path):
    root = tmp_path / "sessions"
    _write_session(root, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)
    (root / "o" / "r" / "pr-7" / "rounds.txt").write_text("2")
    # pr-8: session dir có snapshot nhưng chưa có findings, không có lock sống
    # → review bị gián đoạn → failed
    d8 = root / "o" / "r" / "pr-8"
    d8.mkdir(parents=True)
    (d8 / "snapshot.json").write_text(json.dumps(SNAPSHOT))

    open_prs = [
        {"number": 7, "title": "T7", "draft": False},
        {"number": 8, "title": "T8", "draft": True},
        {"number": 9, "title": "T9", "draft": False},
    ]

    def fake_gh(args, **kw):
        assert "pulls" in args[1]
        return open_prs

    rows = metrics.open_prs(root, "o", "r", gh=fake_gh)
    by_num = {r["pr"]: r for r in rows}
    assert by_num[7]["status"] == "reviewed"
    assert by_num[7]["rounds"] == 2
    assert by_num[7]["draft"] is False
    assert by_num[8]["status"] == "failed"
    assert by_num[9]["status"] == "not_reviewed"
    # sort theo number desc
    assert [r["pr"] for r in rows] == [9, 8, 7]


def test_open_prs_gh_failure(tmp_path):
    root = tmp_path / "sessions"
    _write_session(root, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)

    def fake_gh(args, **kw):
        raise RuntimeError("rate limited")

    rows = metrics.open_prs(root, "o", "r", gh=fake_gh)
    # gh lỗi → trả PR đã review từ sessions, đánh dấu unavailable
    assert rows[0]["pr"] == 7
    assert rows[0]["unavailable"] is True


def test_open_prs_reviewing_with_existing_findings(tmp_path):
    # PR đã review nhưng lock sống (re-review đang chạy) → vẫn hiện reviewing
    root = tmp_path / "sessions"
    _write_session(root, "o", "r", 7, snapshot=SNAPSHOT,
                   findings=EMPTY_FINDINGS)
    lock = root / "o" / "r" / "pr-7" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    import os
    lock.write_text('{"pid": %d, "started_at": "2026-08-16T10:00:00"}'
                    % os.getpid())

    rows = metrics.open_prs(
        root, "o", "r",
        gh=lambda args, **kw: [{"number": 7, "title": "T7", "draft": False}])
    assert rows[0]["status"] == "reviewing"
    assert rows[0]["pid"] == os.getpid()
