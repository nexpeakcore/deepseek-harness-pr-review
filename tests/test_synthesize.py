from src.synthesize import (_overall_verdict, build_comment, build_ping,
                            build_report, find_report_comment, post_comment,
                            post_ping, summary_counts)

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


def _snap(**kw):
    base = {"owner": "o", "repo": "r", "pr": 9, "title": "t", "body": "",
            "author": "a", "base": "main", "head": "x", "head_sha": "1930e24abc",
            "labels": [], "files": [], "commits": [], "threads": []}
    base.update(kw)
    return base


EMPTY = {"claims": [], "docs": [], "impact": [], "threads": [],
         "unresolved_questions": []}


def test_comment_states_the_review_finished():
    """Comment được PATCH tại chỗ nên GitHub không báo — phải tự nói là đã xong."""
    body = build_comment(_snap(), [], EMPTY, [], rounds=3,
                         completed_at="2026-08-18 11:02 UTC")
    assert "Review complete" in body
    assert "2026-08-18 11:02 UTC" in body     # biết là mới hay cũ
    assert "round 3" in body
    assert "`1930e24`" in body                 # biết review đúng commit nào
    assert "updated in place" in body          # biết vì sao không có notification


def test_completion_line_sits_above_the_collapsed_sections():
    """Phải nằm trên các <details> vì chúng mặc định đóng."""
    body = build_comment(_snap(), [], EMPTY, [], completed_at="x")
    assert body.index("Review complete") < body.index("<details")


def _completion_line_of(body: str) -> str:
    """Chỉ lấy dòng completion — header badge có `background-color` (chứa
    'round') nên assert trên cả header là bẫy."""
    return next(ln for ln in body.splitlines() if ln.startswith("✅"))


def test_completion_line_survives_missing_metadata():
    """Không có rounds.txt / head_sha → vẫn báo hoàn thành, không vỡ."""
    body = build_comment(_snap(head_sha=""), [], EMPTY, [], rounds=None,
                         completed_at="2026-08-18 11:02 UTC")
    line = _completion_line_of(body)
    assert "Review complete" in line
    assert "2026-08-18 11:02 UTC" in line
    assert "round" not in line
    assert "commit `" not in line


def test_completion_timestamp_defaults_to_now():
    import time

    body = build_comment(_snap(), [], EMPTY, [])
    assert time.strftime("%Y-%m-%d", time.gmtime()) in body
    assert "UTC" in body


# --- round ping -------------------------------------------------------------

PING_FINDINGS = {
    "claims": [{"status": "PASS"}] * 12 + [{"status": "PARTIAL"}]
              + [{"status": "UNVERIFIED"}] * 2,
    "docs": [{"status": "STALE"}, {"status": "WRONG"}, {"status": "MATCH"}],
    "impact": [{"impact": "RISK"}, {"impact": "CHANGED"}],
    "threads": [], "unresolved_questions": [],
}


def test_ping_marker_is_not_confused_with_main_marker():
    """post_comment PATCH comment đầu tiên chứa MARKER.

    Nếu MARKER là substring của PING_MARKER, báo cáo đầy đủ sẽ đè lên ping và
    ping vòng trước biến mất. Ràng buộc này phải được ghim lại.
    """
    from src.synthesize import MARKER, PING_MARKER

    assert MARKER not in PING_MARKER
    assert PING_MARKER not in MARKER
    ping = build_ping(_snap(), PING_FINDINGS, rounds=1, completed_at="t")
    assert MARKER not in ping


def test_ping_carries_the_headline_numbers():
    ping = build_ping(_snap(), PING_FINDINGS, rounds=3,
                      completed_at="2026-08-18 11:02 UTC")
    assert "round 3" in ping
    assert "`1930e24`" in ping            # commit đã review
    assert "2026-08-18 11:02 UTC" in ping
    assert "**2** risks" in ping          # 1 PARTIAL + 1 RISK impact
    assert "**2** doc errors" in ping     # STALE + WRONG, MATCH không tính
    assert "15 claims" in ping
    assert "12 matches" in ping and "1 partial" in ping and "2 unverified" in ping


def test_ping_numbers_match_the_full_comment():
    """Ping và comment đầy đủ phải cùng một nguồn số, không được lệch."""
    counts = summary_counts(PING_FINDINGS)
    body = build_comment(_snap(), [], PING_FINDINGS, [], completed_at="t")
    ping = build_ping(_snap(), PING_FINDINGS, completed_at="t")
    assert f"Risks found: {counts['risks']}" in body
    assert f"**{counts['risks']}** risk" in ping
    assert f"Doc errors: {counts['doc_errors']}" in body
    assert f"**{counts['doc_errors']}** doc error" in ping


def test_ping_singular_plural():
    one = {"claims": [{"status": "PARTIAL"}], "docs": [{"status": "STALE"}],
           "impact": [], "threads": [], "unresolved_questions": []}
    ping = build_ping(_snap(), one, completed_at="t")
    assert "**1** risk ·" in ping and "**1** risks" not in ping
    assert "**1** doc error ·" in ping
    assert "1 claim (" in ping


def test_ping_posts_a_new_comment_every_round():
    """Ping KHÔNG idempotent — comment mới mỗi vòng mới có notification."""
    seen = []
    post_ping("demo", "app", 7, "body-round-1", gh=lambda a, **k: seen.append(a))
    post_ping("demo", "app", 7, "body-round-2", gh=lambda a, **k: seen.append(a))
    assert len(seen) == 2
    for args in seen:
        assert args[:2] == ["api", "repos/demo/app/issues/7/comments"]
        assert "PATCH" not in args


def test_find_report_comment_ignores_pings():
    from src.synthesize import MARKER, PING_MARKER

    comments = [{"id": 1, "body": f"ping\n{PING_MARKER}", "html_url": "u1"},
                {"id": 2, "body": f"report\n{MARKER}", "html_url": "u2"}]
    found = find_report_comment("demo", "app", 7,
                                list_comments=lambda: comments)
    assert found["id"] == 2


def test_post_comment_updates_report_not_ping():
    """Có ping trong thread → PATCH vẫn phải trúng comment báo cáo."""
    from src.synthesize import MARKER, PING_MARKER

    comments = [{"id": 1, "body": f"ping\n{PING_MARKER}"},
                {"id": 2, "body": f"report\n{MARKER}"}]
    seen = []
    posted = post_comment("demo", "app", 7, "new", gh=lambda a, **k: seen.append(a),
                          list_comments=lambda: comments)
    assert posted is False
    assert "repos/demo/app/issues/comments/2" in seen[0][1]
