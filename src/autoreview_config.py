"""Load, validate and edit autoreview.yml config (single source of truth)."""
import os
from pathlib import Path

import yaml

from src import agent_pool

# Trần cứng cho max_parallel. Không phải giới hạn kỹ thuật mà là chặn tai nạn:
# mỗi review là một agent gọi model, đặt nhầm 100 là đốt quota trong một pass.
MAX_PARALLEL_CAP = 8

DEFAULTS = {
    "org": "",
    "default_mode": "manual",
    "interval_minutes": 2,
    "post_comment": True,
    "skip_human": True,
    "drafts": False,
    "skip_bots": True,
    # Comment ngắn mỗi vòng: thứ duy nhất thật sự tạo notification, vì
    # comment báo cáo đầy đủ được sửa tại chỗ và GitHub không báo khi edit.
    "ping_comment": True,
    # Reviews đồng thời trong 1 pass. Mặc định 1 = tuần tự (hành vi cũ).
    # Mỗi review là 1 tiến trình riêng nên an toàn khi >1; trần thực tế là
    # API concurrency chứ không phải CPU (~80% thời gian là chờ model).
    "max_parallel": 1,
    # Một review treo không được giữ slot mãi khi chạy song song.
    "review_timeout_minutes": 30,
    # Trần agent đồng thời trên TOÀN hệ thống, không phải mỗi review. Mỗi
    # review fan-out nhiều agent, nên số call model thực tế là
    # max_parallel × số agent — trần thật là API concurrency. src/agent_pool.py
    # thực thi qua slot file nên nhiều tiến trình review cùng đếm một quỹ.
    "max_agents": agent_pool.DEFAULT_MAX_AGENTS,
}


def load_config(path: Path) -> dict:
    """Read autoreview.yml, normalize, merge defaults. Raises OSError/ValueError."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid config YAML: {e}") from e
    cfg = {**DEFAULTS, **raw}
    cfg["repos"] = _normalize_repos(cfg.get("repos") or {})
    validate_config(cfg)
    return cfg


def _normalize_repos(repos) -> dict:
    """dict {name: mode} → as-is; legacy list ['owner/repo'] → all auto."""
    if isinstance(repos, dict):
        return {str(k): str(v) for k, v in repos.items()}
    if isinstance(repos, list):
        return {str(r): "auto" for r in repos}
    return {}


def validate_config(cfg: dict) -> None:
    for name, mode in cfg.get("repos", {}).items():
        if mode not in ("auto", "manual"):
            raise ValueError(f"repo mode must be auto|manual: {name!r} -> {mode!r}")
    interval = cfg.get("interval_minutes")
    if not isinstance(interval, int) or interval <= 0:
        raise ValueError("interval_minutes must be a positive integer")
    parallel = cfg.get("max_parallel")
    if not isinstance(parallel, int) or isinstance(parallel, bool) \
            or not 1 <= parallel <= MAX_PARALLEL_CAP:
        raise ValueError(
            f"max_parallel must be an integer 1..{MAX_PARALLEL_CAP}")
    timeout = cfg.get("review_timeout_minutes")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("review_timeout_minutes must be a positive integer")
    agents = cfg.get("max_agents")
    if not isinstance(agents, int) or isinstance(agents, bool) \
            or not 1 <= agents <= agent_pool.MAX_AGENTS_CAP:
        raise ValueError(
            f"max_agents must be an integer 1..{agent_pool.MAX_AGENTS_CAP}")


def _write_atomic(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False))
    os.replace(tmp, path)


def set_repo_mode(path: Path, repo: str, mode: str) -> dict:
    """Add or change mode for a repo. Returns the updated config."""
    if mode not in ("auto", "manual"):
        raise ValueError(f"mode must be auto|manual: {mode!r}")
    cfg = load_config(path)
    cfg["repos"][repo] = mode
    _write_atomic(path, cfg)
    return cfg


def remove_repo(path: Path, repo: str) -> dict:
    """Remove a repo (match full key or by repo name). Returns updated config."""
    cfg = load_config(path)
    if repo in cfg["repos"]:
        del cfg["repos"][repo]
    else:
        for key in list(cfg["repos"]):
            if key.split("/")[-1] == repo.split("/")[-1]:
                del cfg["repos"][key]
    _write_atomic(path, cfg)
    return cfg


def auto_repos(cfg: dict) -> list[tuple[str, str]]:
    """(owner, repo) pairs to auto-review, in config order."""
    pairs = []
    for name, mode in cfg.get("repos", {}).items():
        if mode != "auto":
            continue
        if "/" in name:
            owner, repo = name.split("/", 1)
        else:
            owner, repo = cfg.get("org", ""), name
        if owner and repo:
            pairs.append((owner, repo))
    return pairs


def list_repos(path: Path, gh=None) -> list[dict]:
    """[{name, mode}] — configured repos + org repos (unlisted) when org set.

    gh must be callable like run_gh(args, **kw); default imports gh.run_gh.
    Org lookup failure is silent (returns configured repos only).
    """
    cfg = load_config(path)
    if gh is None:
        from src.gh import run_gh
        gh = run_gh
    names = list(cfg["repos"].keys())
    if cfg.get("org"):
        try:
            data = gh(["api", f"orgs/{cfg['org']}/repos", "--paginate"])
            org_names = [r["name"] for r in data if isinstance(r, dict)]
            names = org_names + [n for n in names if n not in org_names]
        except (RuntimeError, OSError):
            pass
    return [{"name": n, "mode": cfg["repos"].get(n, "unlisted")}
            for n in sorted(names)]
