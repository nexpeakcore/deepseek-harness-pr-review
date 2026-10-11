"""Remove review sessions whose PR is no longer open.

A session is `<session_root>/<owner>/<repo>/pr-<n>/`. Each one carries a full
`workspace/` clone, so merged and abandoned PRs pile up hundreds of MB apiece;
nothing else ever deletes them.
"""
import re
import shutil
from pathlib import Path

from src.gh import run_gh
from src.review_proc import review_lock_alive

_PR_DIR = re.compile(r"pr-(\d+)$")


def pr_is_closed(owner: str, repo: str, num: int, gh=run_gh) -> bool | None:
    """True if the PR is closed or merged, False if open, None if GitHub can't say.

    None covers a repo that is gone or unreachable and a gh auth hiccup alike.
    The caller keeps those sessions: deleting on a failed lookup would wipe
    open reviews the first time the token expires.
    """
    try:
        pr = gh(["pr", "view", str(num), "--repo", f"{owner}/{repo}",
                 "--json", "state"])
    except RuntimeError:
        return None
    state = pr.get("state") if isinstance(pr, dict) else None
    if state == "OPEN":
        return False
    if state in ("CLOSED", "MERGED"):
        return True
    return None


def _subdirs(path: Path) -> list[Path]:
    """Real child directories, sorted. Symlinks are never followed or removed."""
    return sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())


def clean_sessions(session_root: Path, *, dry_run: bool = False,
                   gh=run_gh) -> dict:
    """Delete the session of every closed or merged PR under session_root.

    Returns {"removed": [...], "open": int, "busy": [...], "unknown": [...]}.
    A closed PR whose review is still running is `busy` and is left for the
    next pass. With dry_run nothing is touched and `removed` lists what would be.
    """
    result: dict = {"removed": [], "open": 0, "busy": [], "unknown": []}
    if not session_root.is_dir():
        return result
    for owner_dir in _subdirs(session_root):
        for repo_dir in _subdirs(owner_dir):
            for session_dir in _subdirs(repo_dir):
                m = _PR_DIR.fullmatch(session_dir.name)
                if not m:
                    continue
                closed = pr_is_closed(owner_dir.name, repo_dir.name,
                                      int(m.group(1)), gh)
                if closed is None:
                    result["unknown"].append(session_dir)
                elif not closed:
                    result["open"] += 1
                elif review_lock_alive(session_dir / "review.lock"):
                    result["busy"].append(session_dir)
                else:
                    result["removed"].append(session_dir)
                    if not dry_run:
                        shutil.rmtree(session_dir)
            if not dry_run:
                _rmdir_if_empty(repo_dir)
        if not dry_run:
            _rmdir_if_empty(owner_dir)
    return result


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
