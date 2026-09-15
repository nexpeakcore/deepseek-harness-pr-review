import json

import pytest

from src import code_review
from src.code_review import (annotate_patch, apply_verdicts, code_verdict,
                             code_verdict_label, commentable_lines, issue_key,
                             normalize_issues, plan_inline, post_inline_review,
                             render_diff, shard_files)

PATCH = "\n".join([
    "@@ -1,3 +1,4 @@",
    " keep",
    "-old",
    "+new",
    "+added",
    " tail",
    "@@ -20,2 +21,2 @@ def f():",
    "-    return 1",
    "+    return 2",
    "\\ No newline at end of file",
])


# --- the diff an agent reads ------------------------------------------------

def test_commentable_lines_are_the_new_side_of_every_hunk():
    assert commentable_lines(PATCH) == {1, 2, 3, 4, 21}


def test_commentable_lines_of_a_missing_patch_is_empty():
    assert commentable_lines("") == set()


def test_annotated_patch_numbers_the_new_side_only():
    lines = annotate_patch(PATCH).splitlines()
    assert lines[0] == "@@ -1,3 +1,4 @@"
    assert lines[1].split() == ["1", "keep"]
    assert lines[2].split() == ["-old"]           # removed: no new-side number
    assert lines[3].split() == ["2", "+new"]
    assert lines[-1].split() == ["21", "+", "return", "2"]


def test_render_diff_says_when_github_sent_no_patch():
    text = render_diff([{"filename": "logo.png", "status": "modified",
                         "additions": 0, "deletions": 0, "patch": ""}])
    assert "=== logo.png (modified +0/-0)" in text
    assert "GitHub sent no patch" in text


def _files(n, patch="x"):
    return [{"filename": f"f{i}.py", "patch": patch} for i in range(n)]


def test_shard_files_by_count():
    shards = shard_files(_files(45))
    assert [len(s) for s in shards] == [20, 20, 5]
    assert [f["filename"] for s in shards for f in s] == [f"f{i}.py" for i in range(45)]


def test_shard_files_by_patch_size(monkeypatch):
    monkeypatch.setattr(code_review, "CODE_SHARD_PATCH_CHARS", 100)
    shards = shard_files(_files(5, patch="y" * 60))
    assert [len(s) for s in shards] == [1, 1, 1, 1, 1]


def test_shard_files_keeps_a_single_oversized_file():
    """One huge file still gets reviewed — alone, not dropped."""
    shards = shard_files(_files(1, patch="z" * (code_review.CODE_SHARD_PATCH_CHARS + 1)))
    assert [len(s) for s in shards] == [1]


def test_shard_files_of_nothing():
    assert shard_files([]) == []


# --- issues -----------------------------------------------------------------

def _issue(**kw):
    base = {"file": "a.py", "line": 2, "severity": "MAJOR",
            "category": "correctness", "title": "off by one",
            "scenario": "n=0 returns -1", "evidence": ["a.py:2"]}
    base.update(kw)
    return base


def test_normalize_issues_numbers_across_shards_and_drops_junk():
    issues = normalize_issues([
        _issue(), "junk", _issue(severity="nit"), _issue(title=""),
        _issue(file="./b.py", severity="blocker", category="weird", line="x"),
    ])
    assert [i["id"] for i in issues] == ["K1", "K2"]
    assert issues[1]["file"] == "b.py"
    assert issues[1]["severity"] == "BLOCKER"
    assert issues[1]["category"] == "correctness"   # unknown category folded
    assert issues[1]["line"] == 0


def test_apply_verdicts_moves_rejected_out_with_the_reason():
    issues = normalize_issues([_issue(), _issue(severity="BLOCKER", title="race"),
                               _issue(severity="MINOR", title="leak"),
                               _issue(title="skipped by verifier")])
    kept, rejected = apply_verdicts(issues, [
        {"id": "K1", "verdict": "CONFIRMED", "reason": "traced"},
        {"id": "K2", "verdict": "REJECTED", "reason": "guarded by caller"},
    ])
    assert [i["id"] for i in kept] == ["K1", "K3", "K4"]
    assert kept[0]["verified"] is True
    assert kept[1]["verified"] is None     # MINOR is never checked
    assert kept[2]["verified"] is None     # verifier left it out: unconfirmed
    assert rejected == [{**issues[1], "reason": "guarded by caller"}]


def test_apply_verdicts_without_a_verifier_keeps_everything_unconfirmed():
    kept, rejected = apply_verdicts(normalize_issues([_issue()]), None)
    assert rejected == []
    assert kept[0]["verified"] is None


def test_a_rejected_minor_is_kept():
    """Only what the verifier was asked about can be rejected by it."""
    kept, rejected = apply_verdicts(normalize_issues([_issue(severity="MINOR")]),
                                    [{"id": "K1", "verdict": "REJECTED"}])
    assert len(kept) == 1 and rejected == []


# --- verdict ----------------------------------------------------------------

def test_a_session_without_the_code_axis_is_not_reviewed_not_clean():
    findings = {"claims": []}
    assert code_verdict(findings) == "NOT_RUN"
    assert code_verdict_label(findings) == "Code: not reviewed"


def test_every_code_agent_dead_is_not_reviewed_not_clean():
    findings = {"code": [], "code_meta": {"shards": 2, "failed_shards": 2}}
    assert code_verdict(findings) == "NOT_RUN"


def test_code_verdict_is_the_worst_severity():
    base = {"code_meta": {"shards": 1, "failed_shards": 0}}
    assert code_verdict({**base, "code": []}) == "CLEAN"
    assert code_verdict({**base, "code": [_issue(severity="MINOR")]}) == "CLEAN"
    assert code_verdict({**base, "code": [_issue()]}) == "MAJOR"
    assert code_verdict({**base, "code": [_issue(), _issue(severity="BLOCKER")]}) == "BLOCKER"


@pytest.mark.parametrize("severities, label", [
    ([], "Code: no issues found"),
    (["MINOR", "MINOR"], "Code: no blocking issues (2 minor)"),
    (["BLOCKER", "MAJOR", "MAJOR", "MINOR"], "Code: 1 blocker · 2 major · 1 minor"),
])
def test_code_verdict_label(severities, label):
    findings = {"code": [_issue(severity=s) for s in severities],
                "code_meta": {"shards": 1, "failed_shards": 0}}
    assert code_verdict_label(findings) == label


def test_a_partial_code_review_says_so():
    findings = {"code": [_issue()], "code_meta": {"shards": 2, "failed_shards": 1}}
    assert code_verdict_label(findings) == "Code: 1 major (partial)"


# --- inline comments --------------------------------------------------------

FILES = [{"filename": "a.py", "patch": PATCH}]


def _confirmed(**kw):
    return {**_issue(**kw), "id": "K1", "verified": True}


def test_issue_key_ignores_the_line_and_title_noise():
    assert issue_key(_issue(line=2)) == issue_key(_issue(line=40))
    assert issue_key(_issue(title="Off-by-one!")) == issue_key(_issue(title="off by one"))
    assert issue_key(_issue()) != issue_key(_issue(file="b.py"))


def test_plan_inline_posts_confirmed_blocker_and_major_in_the_diff():
    comments, skipped = plan_inline([_confirmed(line=3)], FILES, [])
    assert comments == [{"path": "a.py", "line": 3, "side": "RIGHT",
                         "body": comments[0]["body"]}]
    body = comments[0]["body"]
    assert "MAJOR · correctness" in body and "off by one" in body
    assert "n=0 returns -1" in body
    assert f"<!-- harness-code:{issue_key(_issue())} -->" in body
    assert skipped == {"unconfirmed": 0, "outside_diff": 0, "already_posted": 0}


def test_plan_inline_keeps_everything_else_in_the_report():
    issues = [
        _confirmed(severity="MINOR"),                    # never inline
        {**_issue(title="unconfirmed"), "verified": None},
        _confirmed(title="outside", line=10),            # not in a hunk
        _confirmed(title="other file", file="zzz.py"),
    ]
    comments, skipped = plan_inline(issues, FILES, [])
    assert comments == []
    assert skipped == {"unconfirmed": 1, "outside_diff": 2, "already_posted": 0}


def test_plan_inline_never_posts_the_same_issue_twice():
    first, _ = plan_inline([_confirmed(line=3)], FILES, [])
    existing = [{"path": "a.py", "line": 3, "body": first[0]["body"]}]
    # Next round: the same defect, now one line lower after a push above it.
    again, skipped = plan_inline([_confirmed(line=4)], FILES, existing)
    assert again == [] and skipped["already_posted"] == 1


def test_plan_inline_skips_a_line_it_already_commented_on():
    existing = [{"path": "a.py", "line": 3, "original_line": 3,
                 "body": "x <!-- harness-code:0123456789ab -->"}]
    comments, skipped = plan_inline([_confirmed(line=3, title="reworded")],
                                    FILES, existing)
    assert comments == [] and skipped["already_posted"] == 1


def test_plan_inline_ignores_human_comments_on_the_same_line():
    existing = [{"path": "a.py", "line": 3, "body": "nit: rename this"}]
    comments, _ = plan_inline([_confirmed(line=3)], FILES, existing)
    assert len(comments) == 1


def test_plan_inline_dedupes_within_one_round():
    comments, skipped = plan_inline([_confirmed(line=3), _confirmed(line=3)],
                                    FILES, [])
    assert len(comments) == 1 and skipped["already_posted"] == 1


class _FakeGh:
    def __init__(self, existing=()):
        self.calls, self.payloads, self.existing = [], [], list(existing)

    def __call__(self, args, **kw):
        self.calls.append(args)
        if "--input" in args:
            with open(args[args.index("--input") + 1]) as f:
                self.payloads.append(json.load(f))
            return {"id": 1}
        return self.existing


def _findings(*issues):
    return {"code": list(issues), "code_meta": {"shards": 1, "failed_shards": 0}}


def test_post_inline_review_posts_one_comment_review_on_the_head():
    gh = _FakeGh()
    result = post_inline_review("o", "r", 7, {"files": FILES, "head_sha": "abc123"},
                                _findings(_confirmed(line=3)), gh=gh)
    assert result["posted"] == 1
    assert gh.calls[0][:2] == ["api", "repos/o/r/pulls/7/comments"]
    assert gh.calls[1][:4] == ["api", "repos/o/r/pulls/7/reviews", "-X", "POST"]
    payload = gh.payloads[0]
    assert payload["event"] == "COMMENT"      # advises; never gates the merge
    assert payload["commit_id"] == "abc123"
    assert payload["comments"][0]["path"] == "a.py"
    assert code_review.REVIEW_MARKER in payload["body"]


def test_post_inline_review_posts_nothing_when_nothing_qualifies():
    gh = _FakeGh()
    result = post_inline_review("o", "r", 7, {"files": FILES},
                                _findings(_confirmed(severity="MINOR")), gh=gh)
    assert result["posted"] == 0
    assert not gh.payloads


def test_post_inline_review_skips_a_session_without_the_code_axis():
    gh = _FakeGh()
    assert post_inline_review("o", "r", 7, {"files": FILES}, {}, gh=gh)["posted"] == 0
    assert gh.calls == []
