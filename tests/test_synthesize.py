from src.synthesize import _overall_verdict, build_comment, build_report, post_comment

SNAPSHOT = {
    "owner": "demo", "repo": "app", "pr": 7,
    "title": "Add checkout flow", "author": "dev1", "base": "main", "head": "x",
    "labels": ["feature"], "body": "Adds checkout.",
    "files": [{"filename": "src/checkout.py", "status": "added",
               "additions": 50, "deletions": 0, "patch": ""}],
    "commits": [{"sha": "a", "message": "feat"}],
    "threads": [{"path": "src/checkout.py", "line": 3, "author": "r1",
                 "body": "Missing validation", "resolved": False, "outdated": False}],
}

CLAIMS = [
    {"id": "C1", "text": "Adds checkout", "category": "feature",
     "files": ["src/checkout.py"], "docs": []},
]

FINDINGS = {
    "claims": [{"id": "C1", "status": "PASS", "evidence": ["src/checkout.py:1"], "note": ""}],
    "docs": [{"path": "docs/payment.md", "status": "WRONG", "what": "doc says retry 3, code retries 5"}],
    "impact": [{"requirement": "REQ-1 checkout", "impact": "CHANGED", "detail": "new flow"}],
    "threads": [{"text": "Missing validation", "status": "STILL_VALID", "note": "not fixed yet"}],
    "unresolved_questions": [],
}

ANSWERS = [{"question": "Is the payment doc wrong?", "kind": "doc", "answer": "y"}]


def test_build_report_vn(tmp_path):
    report = build_report(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS, tmp_path)
    assert "## Verdict" in report
    assert "Matches" in report
    assert "WRONG" in report
    assert "REQ-1" in report
    assert "not fixed yet" in report
    assert (tmp_path / "report.md").exists()


def test_build_comment_en_has_marker_and_verdict():
    comment = build_comment(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS)
    assert "<!-- harness-pr-review -->" in comment
    assert "PASS" in comment
    assert "docs/payment.md" in comment
    assert "STILL_VALID" in comment


def test_build_comment_embeds_full_report(tmp_path):
    report = build_report(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS, tmp_path)
    comment = build_comment(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS,
                            report_content=report)
    assert "<!-- harness-pr-review -->" in comment
    assert "Verdict:" in comment
    assert "REQ-1" in comment
    assert "PASS" in comment


def test_build_comment_html_sections():
    comment = build_comment(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS)
    assert "Claims" in comment
    assert "Docs vs reality" in comment
    assert "Requirement impact" in comment
    assert "Review threads" in comment
    assert "Confirm log" in comment
    assert "<details" in comment
    assert "background-color" in comment


def test_post_comment_updates_via_gh(monkeypatch):
    existing = [{"id": 42, "body": "<!-- harness-pr-review --> old"}]
    seen = []
    monkeypatch.setattr("synthesize.run_gh",
                        lambda args, **kw: (existing if "GET" in args else None))

    def fake_gh(args, **kw):
        seen.append(args)
        return {"id": 42}

    posted = post_comment("demo", "app", 7, "new", gh=fake_gh,
                          list_comments=lambda: existing)
    assert posted is False
    # body được gửi qua temp file (-F body=@...) để tránh ARG_MAX
    assert seen[0][:4] == ["api", "repos/demo/app/issues/comments/42",
                           "-X", "PATCH"]
    assert seen[0][4] == "-F"
    assert seen[0][5].startswith("body=@")


def test_post_comment_posts_when_no_marker(monkeypatch):
    posted = post_comment("demo", "app", 7, "new",
                          gh=lambda args, **kw: {"id": 1},
                          list_comments=lambda: [{"body": "other"}])
    assert posted is True


def test_build_report_escapes_cells(tmp_path):
    findings = {
        "claims": [{"id": "C1", "status": "PASS", "evidence": ["a.py:1|2"],
                    "note": "note\nwith | pipe"}],
        "docs": [{"path": "docs/a.md", "status": "WRONG", "what": "diff|erence\nnewline"}],
        "impact": [{"requirement": "REQ-1|a", "impact": "CHANGED", "detail": "de\ntail"}],
        "threads": [{"text": "comment\nwith | pipe", "status": "STILL_VALID", "note": "no\ntes|1"}],
        "unresolved_questions": [],
    }
    report = build_report(SNAPSHOT, CLAIMS, findings, [], tmp_path)
    assert "\\|" in report
    assert "<br>" in report
    assert "Content" in report
    assert "Adds checkout" in report
    comment = build_comment(SNAPSHOT, CLAIMS, findings, [])
    assert "comment" in comment
    assert "no" in comment  # HTML-escaped, không còn raw "| pipe"


def test_no_claims_verdict(tmp_path):
    findings = {"claims": [], "docs": [], "impact": [], "threads": [],
                "unresolved_questions": []}
    report = build_report(SNAPSHOT, [], findings, [], tmp_path)
    assert "NO CLAIMS" in report
    comment = build_comment(SNAPSHOT, [], findings, [])
    assert "No claims" in comment


def test_verdict_precedence():
    assert _overall_verdict({"claims": [{"status": "PASS"}, {"status": "FAIL"}]}) == "MISLEADING"
    assert _overall_verdict({"claims": [{"status": "PASS"}, {"status": "UNVERIFIED"}]}) == "PARTIAL"


def test_post_comment_default_lists_paginated_and_posts_dash_f():
    seen = []

    def fake_gh(args, **kw):
        seen.append(args)
        return []

    post_comment("demo", "app", 7, "body", gh=fake_gh)
    assert "--paginate" in seen[0]
    # body qua temp file để tránh ARG_MAX
    assert "-F" in seen[1]
    assert seen[1][seen[1].index("-F") + 1].startswith("body=@")


def test_build_comment_summary_badges():
    comment = build_comment(SNAPSHOT, CLAIMS, FINDINGS, ANSWERS)
    assert "Risks found:" in comment
    assert "Doc errors:" in comment
    assert "background-color" in comment
