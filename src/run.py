"""CLI entry: orchestrates 5 phase. Usage:
harness-pr-review <owner>/<repo> <pr> [--skip-human] [--force] [--no-post]
                  [--fixtures DIR] [--dry-run]
harness-pr-review doctor
"""
import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from src.config import load_config
from src.gh import gh_available, run_gh
from src.review_proc import review_lock_alive
from src.human_gate import run_gate
from src.synthesize import build_comment, build_report, post_comment
from src.verify import run_verify, setup_workspace


def _doctor() -> int:
    """Check readiness: Python, gh, API key, SDK, config. Exit 0 if ready."""
    import platform

    ok = True
    py = platform.python_version_tuple()
    if (int(py[0]), int(py[1])) >= (3, 10):
        print(f"✓ Python 3.10+ ({platform.python_version()})")
    else:
        print(f"✗ Python 3.10+ required, found {platform.python_version()}")
        ok = False

    if gh_available():
        try:
            user = run_gh(["api", "user"])
            print(f"✓ gh CLI installed + authenticated ({user.get('login', '?')})")
        except RuntimeError:
            print("✗ gh CLI installed but not authenticated — run `gh auth login`")
            ok = False
    else:
        print("✗ gh CLI not installed — install GitHub CLI and run `gh auth login`")
        ok = False

    cfg = load_config()
    if cfg.api_key:
        print("✓ DEEPSEEK_API_KEY set")
    else:
        print("✗ DEEPSEEK_API_KEY not set — see .env.example")
        ok = False

    try:
        importlib.metadata.version("deepseek-harness-sdk")
        print(f"✓ deepseek-harness-sdk installed "
              f"({importlib.metadata.version('deepseek-harness-sdk')})")
    except importlib.metadata.PackageNotFoundError:
        print("✗ deepseek-harness-sdk not installed — run `pip install -e '.[dev]'`")
        ok = False

    from src.autoreview_config import load_config as load_autoreview_config

    config_path = Path("autoreview.yml")
    if config_path.exists():
        try:
            acfg = load_autoreview_config(config_path)
            print(f"✓ autoreview.yml valid ({len(acfg['repos'])} repos)")
        except (ValueError, OSError) as e:
            print(f"✗ autoreview.yml invalid: {e}")
            ok = False
    else:
        print("· autoreview.yml not found (optional — only needed for auto review)")

    if ok:
        print("\nReady. Run: harness-pr-review owner/repo 123")
        print("Web dashboard: harness-pr-review web  →  http://127.0.0.1:6789")
    return 0 if ok else 1


def _web() -> int:
    """Launch the web dashboard on 127.0.0.1:6789."""
    try:
        import uvicorn
    except ModuleNotFoundError:
        print("Web dashboard requires the 'web' extras. "
              "Reinstall with:\n"
              "  pip install -U 'deepseek-harness-pr-review[web] @ "
              "git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git'\n"
              "(or re-run the one-liner installer, which includes them)",
              file=sys.stderr)
        return 1

    from web.server import app

    uvicorn.run(app, host="127.0.0.1", port=6789)
    return 0


def _load_or_skip(name: str, session_dir: Path, force: bool) -> dict | list | None:
    path = session_dir / name
    if path.exists() and not force:
        return json.loads(path.read_text())
    return None


def _write_failed_report(session_dir: Path, error: Exception) -> None:
    lines = [
        "# Review FAILED",
        "",
        f"- Error: {error}",
        f"- Failed phase: see stderr",
        f"- Existing artifacts: {[p.name for p in sorted(session_dir.iterdir()) if p.is_file()]}",
        "",
    ]
    (session_dir / "report.md").write_text("\n".join(lines))


def _read_rounds(session_dir: Path) -> int | None:
    """Current review-round count, or None if unknown/unreadable."""
    try:
        return int((session_dir / "rounds.txt").read_text().strip())
    except (OSError, ValueError):
        return None


def _bump_rounds(session_dir: Path) -> None:
    """Increment the review-round counter for a session (after a verify pass)."""
    path = session_dir / "rounds.txt"
    try:
        current = int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        current = 0
    path.write_text(str(current + 1))


def _review_lock_path(session_dir: Path) -> Path:
    return session_dir / "review.lock"


def _acquire_review_lock(session_dir: Path) -> bool:
    """Create the per-PR review lock (JSON {pid, started_at}).

    Same file the web dashboard and the autoreview poller read, so manual CLI
    reviews, web-triggered reviews, and the poller mutually exclude each other
    on the same PR (shared workspace, shared comment). Returns False if a live
    review already holds the lock.

    A lock whose PID is dead is stale (review crashed / was SIGKILLed) and is
    reclaimed: otherwise one hard crash would wedge that PR forever for the
    CLI and the poller, which call this directly and have no dashboard to
    clean up after them.
    """
    import os as _os
    import time as _time

    lock = _review_lock_path(session_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = _os.open(lock, _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        except FileExistsError:
            if attempt == 2 or review_lock_alive(lock):
                return False
            print("[harness] reclaiming stale review.lock (PID dead — "
                  "previous review crashed)", file=sys.stderr)
            try:
                lock.unlink()
            except FileNotFoundError:
                pass  # another process won the race; retry decides
            except OSError:
                return False
            continue
        with _os.fdopen(fd, "w") as f:
            json.dump({"pid": _os.getpid(),
                       "started_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
        return True
    return False


def _release_review_lock(session_dir: Path) -> None:
    try:
        _review_lock_path(session_dir).unlink()
    except FileNotFoundError:
        pass


def _version() -> int:
    """Print the installed version."""
    try:
        ver = importlib.metadata.version("deepseek-harness-pr-review")
    except importlib.metadata.PackageNotFoundError:
        ver = "dev (not installed via pip)"
    print(ver)
    return 0


def _update() -> int:
    """Self-update from GitHub: pip install -U git+URL."""
    import subprocess

    try:
        old = importlib.metadata.version("deepseek-harness-pr-review")
    except importlib.metadata.PackageNotFoundError:
        old = "dev (not installed via pip)"
    print(f"Current version: {old}")
    print("Updating from GitHub...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U",
         "deepseek-harness-pr-review[web] @ "
         "git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"Update failed:\n{proc.stderr[-2000:]}", file=sys.stderr)
        return 1
    try:
        new = importlib.metadata.version("deepseek-harness-pr-review")
    except importlib.metadata.PackageNotFoundError:
        new = "?"
    print(f"Updated: {old} → {new}")
    print("Note: restart a running web dashboard to pick up the new code.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness-pr-review")
    parser.add_argument("pr", nargs="?", help="<owner>/<repo> <pr-number> or owner/repo#n")
    parser.add_argument("number", nargs="?", type=int,
                        help="PR number (omit if pr = owner/repo#n)")
    parser.add_argument("--skip-human", action="store_true",
                        help="don't ask, mark as SKIPPED")
    parser.add_argument("--force", action="store_true",
                        help="re-run phases that already have results")
    parser.add_argument("--no-post", action="store_true",
                        help="don't post a comment on the PR")
    parser.add_argument("--dry-run", action="store_true",
                        help="only build the report, don't post")
    parser.add_argument("--fixtures", type=Path, default=None,
                        help="directory containing snapshot.json/claims.json/findings.json "
                             "(for e2e, skips gh & model)")
    parser.add_argument("--version", action="store_true",
                        help="print the installed version")
    parser.add_argument("doctor", nargs="?", help="check readiness (Python, gh, API key, SDK)")
    parser.add_argument("update", nargs="?", help="self-update from GitHub")
    parser.add_argument("web", nargs="?", help="open the web dashboard at http://127.0.0.1:6789")
    args = parser.parse_args(argv)

    if args.version:
        return _version()
    if args.doctor == "doctor" or args.pr == "doctor":
        return _doctor()
    if args.update == "update" or args.pr == "update":
        return _update()
    if args.web == "web" or args.pr == "web":
        return _web()
    if args.pr is None:
        parser.print_help()
        return 2

    from src.repo_ref import parse_pr, parse_repo

    try:
        if "#" in args.pr or "/pull/" in args.pr:
            owner, repo, pr_num = parse_pr(args.pr)
        elif args.number is not None:
            owner, repo = parse_repo(args.pr)
            pr_num = args.number
        else:
            owner, repo, pr_num = parse_pr(args.pr)
        num = str(pr_num)
    except ValueError as e:
        print(f"usage: harness-pr-review <owner>/<repo> <pr-number> "
              f"(or GitHub URL / owner/repo#n)", file=sys.stderr)
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not num.isdigit():
        print(f"invalid PR number: {num}", file=sys.stderr)
        return 2

    cfg = load_config()
    if not cfg.api_key and args.fixtures is None:
        print("DEEPSEEK_API_KEY not set (see .env.example)", file=sys.stderr)
        return 3
    if not gh_available() and args.fixtures is None:
        print("gh CLI not installed or not authenticated (gh auth login)", file=sys.stderr)
        return 2

    session_dir = cfg.session_root / owner / repo / f"pr-{num}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # Per-PR lock: manual CLI, web trigger, và poller loại trừ nhau trên cùng
    # một PR (workspace dùng chung, comment idempotent). Fixtures mode không
    # chạy verify nên không cần lock.
    locked = False
    if args.fixtures is None:
        if not _acquire_review_lock(session_dir):
            print("review already running for this PR "
                  "(review.lock held)", file=sys.stderr)
            return 1
        locked = True

    try:
        if args.fixtures is not None:
            for name in ("snapshot.json", "claims.json", "findings.json"):
                src = args.fixtures / name
                if not src.exists():
                    print(f"missing fixture: {src}", file=sys.stderr)
                    return 2
                (session_dir / name).write_text(src.read_text())
            findings = json.loads((session_dir / "findings.json").read_text())
        else:
            from src.snapshot import build_snapshot
            from src.claims import extract_claims

            snapshot = _load_or_skip("snapshot.json", session_dir, args.force)
            if snapshot is None:
                snapshot = build_snapshot(owner, repo, int(num), session_dir)
            claims = _load_or_skip("claims.json", session_dir, args.force)
            if claims is None:
                claims = extract_claims(
                    snapshot, {"model": cfg.model, "api_key": cfg.api_key,
                               "base_url": cfg.base_url}, session_dir)
            findings = _load_or_skip("findings.json", session_dir, args.force)
            if findings is None:
                workspace = session_dir / "workspace"
                setup_workspace(owner, repo, int(num), workspace)
                findings = run_verify(
                    {"model": cfg.model}, workspace, session_dir, snapshot, claims)
                (session_dir / "findings.json").write_text(
                    json.dumps(findings, indent=2))
                _bump_rounds(session_dir)

        answers = _load_or_skip("answers.json", session_dir, args.force)
        if answers is None:
            answers = run_gate(findings, session_dir,
                               interactive=not args.skip_human)

        snapshot = json.loads((session_dir / "snapshot.json").read_text())
        claims = json.loads((session_dir / "claims.json").read_text())
        report = build_report(snapshot, claims, findings, answers, session_dir)
        print(f"Report: {session_dir / 'report.md'}")

        if args.dry_run or args.fixtures is not None or args.no_post:
            return 0
        body = build_comment(snapshot, claims, findings, answers,
                             report_content=report,
                             rounds=_read_rounds(session_dir))
        if post_comment(owner, repo, int(num), body):
            print("Posted comment to PR.")
        else:
            print("Comment exists — updated with full report.")
        return 0
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        _write_failed_report(session_dir, e)
        return 1
    finally:
        if locked:
            _release_review_lock(session_dir)


if __name__ == "__main__":
    sys.exit(main())
