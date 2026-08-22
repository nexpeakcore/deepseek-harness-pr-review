"""FastAPI app: PR review dashboard + repo auto/manual config management."""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.autoreview_config import load_config as load_autoreview_config
from src.autoreview_config import auto_repos, list_repos, remove_repo, set_repo_mode
from src.autoreview_config import repo_mode as config_repo_mode
from src.config import load_config
from src.repo_check import ACTIONABLE, OK, UNKNOWN, check_repo, check_repos
from src.review_proc import EXIT_TIMEOUT, run_review
from web import metrics

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def project_repo() -> dict | None:
    """{"name", "url"} for this tool's own repo, or None if unknown.

    Read from the installed distribution's Project-URL rather than hardcoded, so
    a fork shows its own repo instead of this one. An install predating the
    metadata entry simply gets no link — the header must not depend on it.
    """
    import importlib.metadata

    try:
        urls = importlib.metadata.metadata(
            "deepseek-harness-pr-review").get_all("Project-URL") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("repository", "source", "homepage"):
            url = url.strip()
            return {"name": url.rstrip("/").removeprefix(
                "https://github.com/"), "url": url}
    return None


# Set once so base.html can use it without every route threading it through.
templates.env.globals["project_repo"] = project_repo()

app = FastAPI(title="PR Review Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def _session_root() -> Path:
    return load_config().session_root


def _config_path() -> Path:
    return Path(os.environ.get("AUTOREVIEW_CONFIG", "autoreview.yml"))


def _split(name: str, org: str | None) -> tuple[str, str]:
    """"owner/repo" as-is; a bare name against the configured org."""
    if "/" in name:
        owner, _, repo = name.partition("/")
        return owner, repo
    return (org or ""), name


# Reachability is checked on every /config load, so it is cached: the page is
# refreshed after each edit and 13 repos would otherwise be 13 API calls a time.
_CHECK_TTL_SECONDS = 300
_check_cache: dict[str, tuple[float, dict]] = {}


def _repo_status(names: list[str], org: str | None, *, refresh: bool = False) -> dict:
    """{name: {status, detail}} for configured repos, cached for _CHECK_TTL_SECONDS."""
    import time

    now = time.monotonic()
    if refresh:
        _check_cache.clear()
    fresh, stale = {}, []
    for name in names:
        hit = _check_cache.get(name)
        if hit and now - hit[0] < _CHECK_TTL_SECONDS:
            fresh[name] = hit[1]
        else:
            stale.append(name)

    pairs = [(name, _split(name, org)) for name in stale]
    pairs = [(name, owner, repo) for name, (owner, repo) in pairs if owner and repo]
    results = check_repos([(o, r) for _, o, r in pairs])
    for name, owner, repo in pairs:
        result = results.get(f"{owner}/{repo}", {"status": UNKNOWN, "detail": ""})
        _check_cache[name] = (now, result)
        fresh[name] = result
    # A bare name with no org configured cannot be resolved into a repo at all.
    for name in stale:
        fresh.setdefault(name, {"status": UNKNOWN,
                                "detail": "no org configured; use owner/repo"})
    return fresh


@app.get("/", response_class=HTMLResponse)
def repo_list(request: Request):
    root = _session_root()
    repos = []
    for owner, repo in metrics.list_repos(root):
        rec = metrics.repo_record(root, owner, repo)
        if rec is not None:
            rec["has_data"] = True
            repos.append(rec)
    repos.sort(key=lambda r: r["prs_total"], reverse=True)

    # merge auto-configured repos that have no review data yet
    seen = {(r["owner"], r["repo"]) for r in repos}
    path = _config_path()
    if path.exists():
        try:
            cfg = load_autoreview_config(path)
            for owner, repo in auto_repos(cfg):
                if (owner, repo) not in seen:
                    repos.append({
                        "owner": owner, "repo": repo,
                        "prs_total": 0, "bugs_total": 0,
                        "doc_errors_total": 0, "has_data": False,
                        "mode": "auto",
                    })
        except (ValueError, OSError):
            pass

    return templates.TemplateResponse(
        request, "repo_list.html", {"repos": repos})


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, recheck: int = 0):
    cfg_state = None
    path = _config_path()
    if path.exists():
        try:
            cfg = load_autoreview_config(path)
            repos = list_repos(path)
            # Only configured repos are checked. "unlisted" rows come from the
            # org listing, so they exist by construction.
            listed = [r["name"] for r in repos if r["mode"] != "unlisted"]
            status = _repo_status(listed, cfg.get("org"), refresh=bool(recheck))
            for r in repos:
                r["check"] = status.get(r["name"], {"status": OK, "detail": ""})
            cfg_state = {
                "org": cfg.get("org"),
                "interval": cfg.get("interval_minutes"),
                "drafts": cfg.get("drafts"),
                "post_comment": cfg.get("post_comment"),
                "repos": repos,
                "unreachable": sorted(
                    name for name, c in status.items()
                    if c["status"] in ACTIONABLE),
                "config_path": str(path),
            }
        except (ValueError, OSError) as e:
            cfg_state = {"error": str(e)}
    return templates.TemplateResponse(
        request, "config.html", {"cfg": cfg_state})


@app.get("/repos/{owner}/{repo}", response_class=HTMLResponse)
def repo_page(request: Request, owner: str, repo: str):
    from src.gh import run_gh

    rec = metrics.repo_record(_session_root(), owner, repo)
    if rec is None:
        # Chưa có session — repo vẫn render nếu tồn tại trên GitHub (PR open)
        try:
            run_gh(["api", f"repos/{owner}/{repo}"])
        except (RuntimeError, OSError):
            raise HTTPException(status_code=404,
                                detail="Repo not found")
        rec = {"owner": owner, "repo": repo, "prs_total": 0, "bugs_total": 0,
               "doc_errors_total": 0,
               "verdict_count": {v: 0 for v in metrics.VERDICTS},
               "prs": [], "has_data": False}
    else:
        rec["has_data"] = True

    # mode từ autoreview.yml — repo chưa có trong config dùng default_mode (manual)
    repo_mode = "manual"
    mode_configured = False
    cfg_path = _config_path()
    if cfg_path.exists():
        try:
            from src.autoreview_config import load_config as load_acfg

            acfg = load_acfg(cfg_path)
            configured = config_repo_mode(acfg, owner, repo)
            if configured is not None:
                repo_mode, mode_configured = configured, True
            else:
                repo_mode = acfg.get("default_mode", "manual")
        except (ValueError, OSError):
            pass
    rec["mode"] = repo_mode
    rec["mode_configured"] = mode_configured

    verdict_json = json.dumps(rec["verdict_count"])
    open_qs = sum(p["open_questions"] for p in rec["prs"])

    pr_rows = metrics.open_prs(_session_root(), owner, repo, gh=run_gh)
    return templates.TemplateResponse(
        request, "repo.html",
        {"repo": rec, "verdict_json": verdict_json, "open_qs": open_qs,
         "pr_rows": pr_rows})


@app.get("/repos/{owner}/{repo}/pr/{pr}", response_class=HTMLResponse)
def pr_page(request: Request, owner: str, repo: str, pr: int):
    detail = metrics.pr_detail(_session_root(), owner, repo, pr)
    if detail is None:
        session_dir = _session_root() / owner / repo / f"pr-{pr}"
        reviewing = metrics.review_process_info(session_dir)
        if session_dir.exists() and reviewing:
            # session đang review dở (snapshot/claims có, findings chưa)
            from src.gh import run_gh

            try:
                meta = run_gh(["api", f"repos/{owner}/{repo}/pulls/{pr}"])
                pr_info = {"pr": pr, "title": meta.get("title", ""),
                           "author": (meta.get("user") or {}).get("login", ""),
                           "base": (meta.get("base") or {}).get("ref", ""),
                           "head": (meta.get("head") or {}).get("ref", "")}
            except (RuntimeError, OSError):
                pr_info = {"pr": pr, "title": "", "author": "",
                           "base": "", "head": ""}
            return templates.TemplateResponse(
                request, "pr.html",
                {"detail": None, "pr_info": pr_info, "not_reviewed": True,
                 "reviewing": True, "failed": False,
                 "review_pid": reviewing["pid"],
                 "repo_owner": owner, "repo_name": repo})
        # Session tồn tại nhưng chưa có findings và không có lock sống →
        # review bị gián đoạn. Cùng trạng thái repo list hiện "Failed ·
        # interrupted"; hai trang phải nói giống nhau.
        failed = session_dir.exists()
        # PR chưa review → hiện placeholder + nút Review now (title từ gh)
        from src.gh import run_gh

        try:
            meta = run_gh(["api", f"repos/{owner}/{repo}/pulls/{pr}"])
            pr_info = {"pr": pr, "title": meta.get("title", ""),
                       "author": (meta.get("user") or {}).get("login", ""),
                       "base": (meta.get("base") or {}).get("ref", ""),
                       "head": (meta.get("head") or {}).get("ref", "")}
        except (RuntimeError, OSError):
            raise HTTPException(status_code=404,
                                detail="PR not found in sessions")
        return templates.TemplateResponse(
            request, "pr.html",
            {"detail": None, "pr_info": pr_info, "not_reviewed": True,
             "reviewing": False, "failed": failed,
             "repo_owner": owner, "repo_name": repo})
    return templates.TemplateResponse(
        request, "pr.html",
        {"detail": detail, "pr_info": None, "not_reviewed": False,
         "reviewing": False, "failed": False,
         "repo_owner": owner, "repo_name": repo})


@app.get("/api/config")
def api_config():
    path = _config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    try:
        cfg = load_autoreview_config(path)
        repos = list_repos(path)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")
    return {
        "org": cfg.get("org"),
        "default_mode": cfg.get("default_mode"),
        "interval_minutes": cfg.get("interval_minutes"),
        "post_comment": cfg.get("post_comment"),
        "skip_human": cfg.get("skip_human"),
        "drafts": cfg.get("drafts"),
        "repos": repos,
        "config_path": str(path),
    }


@app.post("/api/config/repos/{repo:path}/mode")
def api_set_mode(repo: str, payload: dict):
    path = _config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    try:
        mode = payload.get("mode")
        set_repo_mode(path, repo, mode)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "repo": repo, "mode": mode}


@app.post("/api/config/repos")
def api_add_repo(payload: dict):
    path = _config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    repo = (payload.get("repo") or "").strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    try:
        cfg = load_autoreview_config(path)
        if "/" not in repo and not cfg.get("org"):
            raise HTTPException(
                status_code=400,
                detail="org not set in config; use owner/repo format")
        owner, name = _split(repo, cfg.get("org"))
        # Check before writing. An unreachable name used to become a permanent
        # config entry that the poller retried every pass and that the dashboard
        # rendered as a real repo.
        check = check_repo(owner, name)
        if check["status"] in ACTIONABLE:
            raise HTTPException(
                status_code=400,
                detail=f"{owner}/{name}: {check['detail']}")
        set_repo_mode(path, repo, payload.get("mode", "auto"))
        _check_cache.pop(repo, None)
    except HTTPException:
        raise
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    # An unverifiable repo is still added — a network blip must not block work —
    # but the caller is told the check did not actually pass.
    warning = (f"could not verify {owner}/{name}: {check['detail']}"
               if check["status"] == UNKNOWN else None)
    return {"ok": True, "repo": repo, "warning": warning}


@app.delete("/api/config/repos/{repo:path}")
def api_remove_repo(repo: str):
    path = _config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    try:
        remove_repo(path, repo)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    _check_cache.pop(repo, None)
    return {"ok": True, "repo": repo}


@app.get("/api/config/repos/check")
def api_check_repos(refresh: int = 0):
    """Reachability of every configured repo."""
    path = _config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    try:
        cfg = load_autoreview_config(path)
        listed = [r["name"] for r in list_repos(path) if r["mode"] != "unlisted"]
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")
    status = _repo_status(listed, cfg.get("org"), refresh=bool(refresh))
    return {
        "repos": status,
        "unreachable": sorted(n for n, c in status.items()
                              if c["status"] in ACTIONABLE),
    }


def _review_lock_path(session_root: Path, owner: str, repo: str, n: int) -> Path:
    return session_root / owner / repo / f"pr-{n}" / "review.lock"


def review_status(session_root: Path, owner: str, repo: str, n: int) -> dict:
    """Lock metadata if the review process is alive; stale flag if PID dead."""
    lock = _review_lock_path(session_root, owner, repo, n)
    if not lock.exists():
        return {"running": False, "stale": False}
    try:
        meta = json.loads(lock.read_text())
        pid = int(meta.get("pid", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        pid = 0
    if pid > 0 and _pid_alive(pid):
        started_at = meta.get("started_at", "")
        elapsed = None
        try:
            from datetime import datetime

            started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S")
            elapsed = int((datetime.now() - started).total_seconds())
        except ValueError:
            pass
        return {"running": True, "pid": pid, "started_at": started_at,
                "elapsed_seconds": elapsed}
    return {"running": False, "stale": True}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@app.get("/api/repos/{owner}/{repo}/pr/{pr}/review/status")
def review_status_api(owner: str, repo: str, pr: int):
    return review_status(_session_root(), owner, repo, pr)


@app.get("/api/repos/{owner}/{repo}/pr/{pr}/review/log")
def review_log_api(owner: str, repo: str, pr: int, lines: int = 200):
    path = _session_root() / owner / repo / f"pr-{pr}" / "review.log"
    log_text = ""
    if path.exists():
        try:
            log_text = "\n".join(path.read_text(errors="replace")
                                 .splitlines()[-max(1, min(lines, 2000)):])
        except OSError:
            log_text = ""
    return {"log": log_text,
            "running": review_status(_session_root(), owner, repo, pr)["running"]}


@app.post("/api/repos/{owner}/{repo}/pr/{pr}/review")
def trigger_review(owner: str, repo: str, pr: int):
    """Run a review synchronously using the repo's autoreview config."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        raise HTTPException(status_code=404, detail="autoreview.yml not found")
    try:
        cfg = load_autoreview_config(cfg_path)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")

    env = load_config()
    if not env.api_key:
        raise HTTPException(status_code=400,
                            detail="DEEPSEEK_API_KEY not set (see .env.example)")

    lock = _review_lock_path(_session_root(), owner, repo, pr)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and review_status(_session_root(), owner, repo, pr)["running"]:
        status = review_status(_session_root(), owner, repo, pr)
        raise HTTPException(
            status_code=409,
            detail=(f"review already running — PID {status['pid']}, "
                    f"started {status['started_at']} "
                    f"({status['elapsed_seconds']}s ago)"))
    if lock.exists():  # stale lock (PID chết) → dọn; run.py sẽ tạo lock mới
        try:
            lock.unlink()
        except OSError:
            pass

    # Review chạy trong tiến trình riêng: redirect_stdout cũ sửa sys.stdout của
    # cả interpreter nên hai review song song ghi đè log của nhau. Tiến trình
    # riêng cũng làm review.lock ghi đúng PID của review thay vì PID của server
    # (PID server luôn sống → review chết vẫn bị coi là đang chạy).
    log_path = lock.parent / "review.log"
    try:
        exit_code = run_review(
            owner, repo, pr,
            session_root=_session_root(),
            log_path=log_path,
            force=True,
            skip_human=cfg.get("skip_human", True),
            no_post=not cfg.get("post_comment", True),
            no_ping=not cfg.get("ping_comment", True),
            timeout_seconds=cfg.get("review_timeout_minutes", 30) * 60)
    except Exception:
        # run.py tự dọn review.lock trong finally; nếu nó chưa kịp tạo lock
        # (lỗi trước pipeline), dọn ở đây để tránh lock mồ côi.
        try:
            if lock.exists() and not review_status(_session_root(), owner,
                                                   repo, pr)["running"]:
                lock.unlink()
        except OSError:
            pass
        raise

    if exit_code == EXIT_TIMEOUT:
        raise HTTPException(
            status_code=504,
            detail=(f"review timed out after "
                    f"{cfg.get('review_timeout_minutes', 30)} minutes and was "
                    f"killed — see sessions/{owner}/{repo}/pr-{pr}/review.log"))
    if exit_code != 0:
        raise HTTPException(
            status_code=500,
            detail=f"review failed (exit {exit_code}): "
                   f"check server log / sessions/{owner}/{repo}/pr-{pr}/report.md")
    return {"ok": True, "exit": exit_code,
            "report": f"sessions/{owner}/{repo}/pr-{pr}/report.md"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=6789)
