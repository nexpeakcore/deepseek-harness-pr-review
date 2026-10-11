# tests/test_session_clean.py
import json
import os

from src.session_clean import clean_sessions, pr_is_closed


def _session(root, owner, repo, num):
    d = root / owner / repo / f"pr-{num}"
    (d / "workspace").mkdir(parents=True)
    (d / "report.md").write_text("r")
    return d


def _gh(states):
    """Fake run_gh: states maps 'owner/repo#n' -> gh's state, or an Exception."""
    def gh(args):
        assert args[:2] == ["pr", "view"]
        key = f"{args[args.index('--repo') + 1]}#{args[2]}"
        state = states[key]
        if isinstance(state, Exception):
            raise state
        return {"state": state}
    return gh


def test_pr_is_closed_maps_states():
    for state, expected in [("OPEN", False), ("CLOSED", True),
                            ("MERGED", True), ("WEIRD", None)]:
        assert pr_is_closed("o", "r", 1, lambda args, s=state: {"state": s}) is expected


def test_pr_is_closed_unknown_when_gh_fails():
    def boom(args):
        raise RuntimeError("gh api failed: HTTP 404")
    assert pr_is_closed("o", "r", 1, boom) is None


def test_removes_closed_and_merged_keeps_open(tmp_path):
    merged = _session(tmp_path, "o", "r", 1)
    closed = _session(tmp_path, "o", "r", 2)
    live = _session(tmp_path, "o", "r", 3)
    res = clean_sessions(tmp_path, gh=_gh({
        "o/r#1": "MERGED", "o/r#2": "CLOSED", "o/r#3": "OPEN"}))
    assert sorted(res["removed"]) == [merged, closed]
    assert res["open"] == 1
    assert not merged.exists() and not closed.exists()
    assert live.exists()


def test_keeps_session_when_state_unavailable(tmp_path):
    gone = _session(tmp_path, "o", "gone", 5)
    res = clean_sessions(tmp_path, gh=_gh({"o/gone#5": RuntimeError("404")}))
    assert res["unknown"] == [gone]
    assert res["removed"] == []
    assert gone.exists()


def test_keeps_closed_pr_while_review_is_running(tmp_path):
    busy = _session(tmp_path, "o", "r", 4)
    (busy / "review.lock").write_text(json.dumps({"pid": os.getpid()}))
    res = clean_sessions(tmp_path, gh=_gh({"o/r#4": "MERGED"}))
    assert res["busy"] == [busy]
    assert busy.exists()


def test_stale_lock_does_not_protect_a_closed_pr(tmp_path):
    stale = _session(tmp_path, "o", "r", 6)
    (stale / "review.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345}))
    res = clean_sessions(tmp_path, gh=_gh({"o/r#6": "CLOSED"}))
    assert res["removed"] == [stale]
    assert not stale.exists()


def test_dry_run_deletes_nothing(tmp_path):
    merged = _session(tmp_path, "o", "r", 1)
    res = clean_sessions(tmp_path, dry_run=True, gh=_gh({"o/r#1": "MERGED"}))
    assert res["removed"] == [merged]
    assert (merged / "workspace").is_dir()


def test_prunes_emptied_repo_dir_but_not_one_with_open_prs(tmp_path):
    _session(tmp_path, "o", "done", 1)
    _session(tmp_path, "o", "active", 2)
    clean_sessions(tmp_path, gh=_gh({"o/done#1": "MERGED", "o/active#2": "OPEN"}))
    assert not (tmp_path / "o" / "done").exists()
    assert (tmp_path / "o" / "active" / "pr-2").is_dir()


def test_prunes_owner_dir_once_empty(tmp_path):
    _session(tmp_path, "solo", "r", 1)
    clean_sessions(tmp_path, gh=_gh({"solo/r#1": "CLOSED"}))
    assert not (tmp_path / "solo").exists()
    assert tmp_path.is_dir()


def test_ignores_non_session_entries_and_symlinks(tmp_path):
    keep = tmp_path / "o" / "r" / "notes"
    keep.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f").write_text("x")
    (tmp_path / "o" / "r" / "pr-9").symlink_to(target, target_is_directory=True)

    def never(args):
        raise AssertionError("must not look up non-session entries")
    res = clean_sessions(tmp_path, gh=never)
    assert res == {"removed": [], "open": 0, "busy": [], "unknown": []}
    assert keep.is_dir() and (target / "f").exists()


def test_missing_session_root_is_a_noop(tmp_path):
    res = clean_sessions(tmp_path / "nope", gh=_gh({}))
    assert res["removed"] == []
