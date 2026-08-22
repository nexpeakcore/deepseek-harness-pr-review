"""Local poller: auto-review new PRs / re-review PRs with changed head SHA.

Modes:
  --once      single pass (cron/launchd)
  --daemon    loop with interval_minutes
  --dry-run   print planned reviews without dispatching
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from src.autoreview_config import auto_repos, list_repos, load_config, \
    remove_repo, set_repo_mode
from src.config import PROVIDERS
from src.config import load_config as load_env_config
from src.gh import gh_available, run_gh
from src.review_proc import review_lock_alive

CONFIG_PATH = Path("autoreview.yml")
LOCK_PATH = Path("autoreview.lock")

# Backoff trạng thái lỗi per-repo (repo 404/đã xóa): đếm lỗi liên tiếp, reset
# khi fetch thành công. Sau _REPO_FAILURE_LIMIT lần → log gọn + tiếp tục bỏ qua.
_repo_failures: dict[str, int] = {}
_REPO_FAILURE_LIMIT = 3


def decide_pr(session_root: Path, owner: str, repo: str, n: int,
              head_sha: str) -> str:
    """Return NEW / RE-RUN / SKIP for one PR."""
    session_dir = session_root / owner / repo / f"pr-{n}"
    snapshot_path = session_dir / "snapshot.json"
    if not snapshot_path.exists():
        return "NEW"
    try:
        snapshot = json.loads(snapshot_path.read_text())
    except (json.JSONDecodeError, OSError):
        return "RE-RUN"  # snapshot hỏng → chạy lại cho an toàn
    old_sha = snapshot.get("head_sha", "")
    if old_sha and old_sha == head_sha:
        # head khớp nhưng snapshot mới hơn findings (re-review fail giữa chừng
        # sau khi fetch head mới) → review chưa hoàn thành → chạy lại
        findings_path = session_dir / "findings.json"
        try:
            if snapshot_path.stat().st_mtime > findings_path.stat().st_mtime:
                return "RE-RUN"
        except OSError:
            return "RE-RUN"  # thiếu findings → review dở dang
        return "SKIP"
    return "RE-RUN"


def plan_reviews(session_root: Path, owner: str, repo: str, prs: list[dict],
                 drafts: bool = False, skip_bots: bool = True) -> list[dict]:
    """Return [{pr, head_sha, decision}] for open PRs of one repo.

    Bot PRs (user.type == "Bot") are skipped by default — they are usually
    dependency bots (Renovate/Dependabot). Manual triggers bypass this.
    """
    plans = []
    for p in prs:
        if p.get("draft") and not drafts:
            continue
        if skip_bots and (p.get("user") or {}).get("type") == "Bot":
            print(f"SKIP-BOT {owner}/{repo}#{p['number']} "
                  f"({(p.get('user') or {}).get('login')})")
            continue
        n = p["number"]
        head_sha = (p.get("head") or {}).get("sha", "")
        decision = decide_pr(session_root, owner, repo, n, head_sha)
        plans.append({"pr": n, "head_sha": head_sha, "decision": decision})
    return plans


def fetch_open_prs(owner: str, repo: str, gh=run_gh) -> list[dict]:
    """Open PRs of a repo via gh api (returns raw list items)."""
    return gh(["api", f"repos/{owner}/{repo}/pulls?state=open", "--paginate"])


def _acquire_lock() -> bool:
    # Lock cũ mà PID chết → dọn và giành lại. PID sống nhưng lock quá già
    # (> 4h: một pass bình thường không bao giờ lâu vậy) → PID bị recycle
    # bởi tiến trình khác hoặc tiến trình treo → cướp lock với cảnh báo.
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text().strip() or "0")
            alive = pid > 0
            if alive:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
            if alive and _lock_age_seconds() < _MAX_LOCK_AGE:
                return False
            if alive:
                print(f"[autoreview] stealing stale lock (age "
                      f"{int(_lock_age_seconds())}s, pid {pid} still alive — "
                      f"PID reuse or hung process)", file=sys.stderr)
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        except ValueError:
            # Lock hỏng/truncate (không parse được PID) → không có chủ hợp lệ.
            # Trả False ở đây sẽ kẹt poller vĩnh viễn và im lặng → dọn + cảnh báo.
            print("[autoreview] removing corrupt autoreview.lock "
                  "(unparseable pid)", file=sys.stderr)
            try:
                LOCK_PATH.unlink()
            except OSError:
                return False
        except PermissionError:
            return False
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


_MAX_LOCK_AGE = 4 * 3600  # 4h


def _lock_age_seconds() -> float:
    try:
        return time.time() - LOCK_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def _clean_rerun_session(session_dir: Path) -> None:
    """Xóa kết quả phase cũ trước khi re-review (giữ snapshot)."""
    for name in ("findings.json", "answers.json", "report.md"):
        (session_dir / name).unlink(missing_ok=True)
    # Một log cho mỗi agent kể từ khi verify fan-out; log của vòng trước mà
    # còn lại sẽ khiến người đọc tưởng axis đó đã chạy trong vòng này.
    for log in session_dir.glob("agent-log*.txt"):
        log.unlink(missing_ok=True)


def _dispatch(cfg: dict, owner: str, repo: str, n: int,
              head_sha: str) -> int:
    """Chạy pipeline cho 1 PR trong tiến trình riêng. Trả về exit code.

    Tiến trình riêng thay vì gọi run.main() trực tiếp: đó là điều kiện để
    chạy song song (stdout riêng, module global riêng) và để review.lock ghi
    đúng PID của review chứ không phải PID của poller.
    """
    from src.review_proc import run_review

    session_root = load_env_config().session_root
    session_dir = session_root / owner / repo / f"pr-{n}"
    return run_review(
        owner, repo, n,
        session_root=session_root,
        log_path=session_dir / "review.log",
        force=True,
        skip_human=cfg.get("skip_human", True),
        no_post=not cfg.get("post_comment", True),
        no_ping=not cfg.get("ping_comment", True),
        timeout_seconds=cfg.get("review_timeout_minutes", 30) * 60,
        max_agents=cfg.get("max_agents"))


def run_pass(cfg: dict, session_root: Path, dry_run: bool = False,
             gh=run_gh) -> int:
    """One poll pass over all auto repos. Returns count of dispatched."""
    dispatched = 0
    queued: list[tuple] = []
    for owner, repo in auto_repos(cfg):
        key = f"{owner}/{repo}"
        try:
            prs = fetch_open_prs(owner, repo, gh=gh)
        except RuntimeError as e:
            # Backoff: repo 404/không tồn tại (đã xóa/đổi tên) — đừng spam log
            # mỗi pass. Sau N lần lỗi liên tiếp, chỉ log gọn mỗi pass.
            _repo_failures[key] = _repo_failures.get(key, 0) + 1
            n_fail = _repo_failures[key]
            if n_fail >= _REPO_FAILURE_LIMIT:
                print(f"POLL-ERROR {key}: {e} "
                      f"(skipping: {n_fail} consecutive failures)", file=sys.stderr)
            else:
                print(f"POLL-ERROR {key}: {e}", file=sys.stderr)
            continue
        _repo_failures[key] = 0
        plans = plan_reviews(session_root, owner, repo, prs,
                             drafts=cfg.get("drafts", False),
                             skip_bots=cfg.get("skip_bots", True))
        for plan in plans:
            n = plan["pr"]
            line = f"{plan['decision']} {owner}/{repo}#{n}"
            if plan["decision"] == "SKIP":
                print(line)
                continue
            if dry_run:
                print(f"[dry-run] would review: {line}")
                dispatched += 1
                continue
            session_dir = session_root / owner / repo / f"pr-{n}"
            lock = session_dir / "review.lock"
            if lock.exists():
                if review_lock_alive(lock):
                    print(f"SKIP {owner}/{repo}#{n}: manual review running")
                    continue
                # Lock chết: review trước bị kill (timeout) hoặc crash. Skip ở
                # đây sẽ treo PR vĩnh viễn vì không ai dọn hộ — poller là
                # đường duy nhất chạm tới nó. Cứ dispatch; _acquire_review_lock
                # trong tiến trình con mới là nơi thu hồi lock.
                print(f"STALE-LOCK {owner}/{repo}#{n}: previous review died, "
                      f"retrying")
            if plan["decision"] == "RE-RUN":
                _clean_rerun_session(session_dir)
            print(line)
            queued.append((owner, repo, n, plan["head_sha"]))

    return dispatched + _dispatch_all(cfg, queued)


def _run_one(cfg: dict, owner: str, repo: str, n: int, head_sha: str) -> bool:
    """Dispatch 1 PR, log lỗi. True nếu review thành công."""
    try:
        code = _dispatch(cfg, owner, repo, n, head_sha)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"FAILED {owner}/{repo}#{n}: {e}", file=sys.stderr)
        return False
    if code != 0:
        print(f"FAILED {owner}/{repo}#{n}: exit {code}", file=sys.stderr)
        return False
    return True


def _dispatch_all(cfg: dict, queued: list[tuple]) -> int:
    """Chạy các PR đã lên lịch, tuần tự hoặc song song. Trả về số thành công.

    max_parallel > 1 an toàn vì mỗi review là một tiến trình riêng và
    review.lock là per-PR: hai PR khác nhau không dùng chung workspace hay
    comment. Cùng một PR thì lock vẫn chỉ cho 1 review chạy.
    """
    workers = min(int(cfg.get("max_parallel", 1) or 1), len(queued))
    if workers <= 1:
        return sum(_run_one(cfg, *item) for item in queued)

    from concurrent.futures import ThreadPoolExecutor

    print(f"[autoreview] {len(queued)} reviews, {workers} at a time")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda item: _run_one(cfg, *item), queued))
    return sum(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoreview")
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--daemon", action="store_true", help="loop forever")
    parser.add_argument("--dry-run", action="store_true",
                        help="print plans without dispatching")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="path to autoreview.yml")
    parser.add_argument("--add-repo", metavar="REPO",
                        help="add repo (name or owner/name) and set its mode")
    parser.add_argument("--rm-repo", metavar="REPO", help="remove a repo")
    parser.add_argument("--force-add", action="store_true",
                        help="add the repo even if GitHub cannot see it")
    parser.add_argument("--check-repos", action="store_true",
                        help="report which configured repos GitHub cannot reach")
    parser.add_argument("--mode", choices=["auto", "manual"], default="auto",
                        help="mode for --add-repo (default: auto)")
    parser.add_argument("--repos", action="store_true",
                        help="list configured + org repos with modes")
    args = parser.parse_args(argv)

    if args.add_repo:
        from src.repo_ref import parse_repo

        raw = args.add_repo.strip()
        try:
            owner, repo_name = parse_repo(raw)
            repo_ref_key = f"{owner}/{repo_name}"
        except ValueError:
            if "/" in raw:
                print(f"error: cannot parse repo from: {raw!r}", file=sys.stderr)
                return 2
            repo_ref_key = raw  # tên trần → org từ config

        # Kiểm tra trước khi ghi: một cái tên gõ sai từng thành entry vĩnh viễn
        # trong autoreview.yml mà poller retry mỗi pass.
        from src.repo_check import ACTIONABLE, UNKNOWN, check_repo

        if "/" in repo_ref_key:
            owner, name = repo_ref_key.split("/", 1)
        else:
            try:
                owner, name = load_config(args.config).get("org", ""), repo_ref_key
            except (ValueError, OSError):
                owner, name = "", repo_ref_key
        if owner and name:
            check = check_repo(owner, name)
            if check["status"] in ACTIONABLE and not args.force_add:
                print(f"error: {owner}/{name}: {check['detail']}\n"
                      f"       use --force-add to add it anyway", file=sys.stderr)
                return 2
            if check["status"] == UNKNOWN:
                print(f"warning: could not verify {owner}/{name}: "
                      f"{check['detail']}", file=sys.stderr)

        set_repo_mode(args.config, repo_ref_key, args.mode)
        print(f"{repo_ref_key} -> {args.mode}")
        return 0
    if args.rm_repo:
        remove_repo(args.config, args.rm_repo)
        print(f"removed {args.rm_repo}")
        return 0
    if args.check_repos:
        from src.autoreview_config import list_repos as _list_repos
        from src.repo_check import ACTIONABLE, check_repos

        cfg = load_config(args.config)
        org = cfg.get("org") or ""
        names = [r["name"] for r in _list_repos(args.config)
                 if r["mode"] != "unlisted"]
        pairs = [tuple(n.split("/", 1)) if "/" in n else (org, n) for n in names]
        results = check_repos([(o, r) for o, r in pairs if o and r])
        bad = 0
        for name, (owner, repo_name) in zip(names, pairs):
            result = results.get(f"{owner}/{repo_name}",
                                 {"status": "unknown", "detail": "no org configured"})
            mark = "ok " if result["status"] == "ok" else result["status"]
            if result["status"] in ACTIONABLE:
                bad += 1
            print(f"{mark:<10} {name}"
                  + (f"  — {result['detail']}" if result["status"] != "ok" else ""))
        # Report on stdout so it stays interleaved with the list; the exit
        # code is the part a script reads.
        if bad:
            print(f"\n{bad} unreachable — remove with: "
                  f"autoreview --rm-repo <name>")
        return 1 if bad else 0

    if args.repos:
        for r in list_repos(args.config):
            print(f"{r['name']:<40} {r['mode']}")
        return 0

    cfg = load_config(args.config)
    env = load_env_config()
    if env.provider not in PROVIDERS:
        print(f"unknown HARNESS_PROVIDER={env.provider!r} — expected one of: "
              f"{', '.join(PROVIDERS)}", file=sys.stderr)
        return 2
    if env.needs_deepseek_key and not env.api_key:
        print("DEEPSEEK_API_KEY not set (see .env.example)", file=sys.stderr)
        return 3
    if env.provider == "claude":
        # Checked here and not left to the children: a review that dies in
        # claim extraction leaves a snapshot and no findings, so decide_pr
        # queues the same PR again on the next interval — every interval,
        # forever. That is not hypothetical; a launchd PATH without the
        # per-user bin dir did exactly this.
        from src import claude_cli

        if not claude_cli.available():
            print(f"HARNESS_PROVIDER=claude but the claude CLI is not on PATH "
                  f"— install Claude Code (https://claude.com/claude-code). "
                  f"PATH was: {os.environ.get('PATH', '(unset)')}",
                  file=sys.stderr)
            return 2
    if not gh_available():
        print("gh CLI not installed or not authenticated (gh auth login)",
              file=sys.stderr)
        return 2

    session_root = env.session_root
    while True:
        if not _acquire_lock():
            print("another autoreview process is running — exiting",
                  file=sys.stderr)
            return 1
        try:
            run_pass(cfg, session_root, dry_run=args.dry_run)
        finally:
            _release_lock()
        if args.once or args.dry_run:
            return 0
        time.sleep(cfg["interval_minutes"] * 60)


if __name__ == "__main__":
    sys.exit(main())
