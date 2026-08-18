import json

import pytest

from src.snapshot import build_snapshot


def _gh_fake(registry):
    """Return a run_gh replacement based on the prefix of args."""
    def fake(args, *, json=True):
        key = next(k for k in registry if args[1].startswith(k))
        val = registry[key]
        if isinstance(val, Exception):
            raise val
        return val

    return fake


PR_META = {
    "number": 7,
    "title": "Add checkout flow",
    "body": "Adds checkout. Fixes payment retry. Docs: docs/payment.md",
    "user": {"login": "dev1"},
    "base": {"ref": "main"},
    "head": {"ref": "feature/checkout"},
    "labels": [{"name": "feature"}],
}

PR_FILES = [
    {"filename": "src/checkout.py", "status": "added", "additions": 50, "deletions": 0,
     "patch": "@@ -0,0 +1 @@\n+def checkout():"},
]

PR_THREADS = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {"isResolved": False, "isOutdated": False,
                         "comments": {"nodes": [
                             {"path": "src/checkout.py", "line": 3,
                              "author": {"login": "reviewer1"}, "body": "Missing validation"}]}},
                    ]
                }
            }
        }
    }
}

COMMITS = [{"sha": "abc123", "commit": {"message": "feat: checkout"}}]


def test_build_snapshot(tmp_path):
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": PR_THREADS,
    }
    session_dir = tmp_path / "sessions"
    result = build_snapshot("demo", "app", 7, session_dir, gh=_gh_fake(registry))

    assert result["title"] == "Add checkout flow"
    assert result["body"].startswith("Adds checkout")
    assert result["files"][0]["filename"] == "src/checkout.py"
    assert result["threads"][0]["resolved"] is False
    assert result["commits"][0]["sha"] == "abc123"
    assert (session_dir / "snapshot.json").exists()
    saved = json.loads((session_dir / "snapshot.json").read_text())
    assert saved["pr"] == 7


def test_build_snapshot_no_patch_field(tmp_path):
    files = [{"filename": "src/a.py", "status": "modified", "additions": 1, "deletions": 1}]
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": files,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": PR_THREADS,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert result["files"][0]["patch"] == ""


def test_build_snapshot_graphql_error(tmp_path):
    error_payload = {"errors": [{"message": "rate limit exceeded"}]}
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": error_payload,
    }
    with pytest.raises(RuntimeError, match="graphql errors"):
        build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))


def test_build_snapshot_head_sha(tmp_path):
    meta = {**PR_META, "head": {"ref": "feature/checkout", "sha": "sha123"}}
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": meta,
        "graphql": PR_THREADS,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert result["head_sha"] == "sha123"


def test_build_snapshot_thread_truncation_warns(tmp_path, capsys):
    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": True, "isOutdated": False,
                             "comments": {"nodes": [
                                 {"path": "src/a.py", "line": 1,
                                  "author": {"login": "x"}, "body": "b"}
                                 for _ in range(100)
                             ]}}
                        ]
                    }
                }
            }
        }
    }
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": threads,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert len(result["threads"]) == 100
    assert "truncated" in capsys.readouterr().err


LINKED = {
    "data": {
        "repository": {
            "pullRequest": {
                "closingIssuesReferences": {
                    "nodes": [{"number": 12, "title": "Payments fail",
                               "body": "One retry is not enough."}]
                },
                "reviewThreads": {"nodes": []},
            }
        }
    }
}


def test_build_snapshot_captures_linked_issues(tmp_path):
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": LINKED,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert result["linked_issues"][0]["number"] == 12
    assert result["linked_issues"][0]["body"] == "One retry is not enough."


def test_build_snapshot_no_linked_issues(tmp_path):
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": PR_THREADS,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert result["linked_issues"] == []


def test_build_snapshot_truncates_long_issue_body(tmp_path):
    from src.snapshot import ISSUE_BODY_MAX

    payload = {"data": {"repository": {"pullRequest": {
        "closingIssuesReferences": {"nodes": [
            {"number": 1, "title": "t", "body": "x" * (ISSUE_BODY_MAX + 500)}]},
        "reviewThreads": {"nodes": []}}}}}
    registry = {
        "repos/demo/app/pulls/7/commits": COMMITS,
        "repos/demo/app/pulls/7/files": PR_FILES,
        "repos/demo/app/pulls/7": PR_META,
        "graphql": payload,
    }
    result = build_snapshot("demo", "app", 7, tmp_path, gh=_gh_fake(registry))
    assert len(result["linked_issues"][0]["body"]) == ISSUE_BODY_MAX
