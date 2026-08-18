"""Does this repo exist, and can we see it?

A repo name is added to autoreview.yml by hand or by one click, and nothing ever
checked it. A typo — or a bare name resolved against the wrong `org` — became a
permanent config entry that the poller retried every pass and that showed up on
the dashboard as a real repo with "No reviews yet", indistinguishable from one
that simply has no open PRs.

GitHub answers 404 both for a repo that does not exist and for a private repo
the token cannot see; it does not tell the two apart, so neither do we.
"""
import re
from concurrent.futures import ThreadPoolExecutor

OK = "ok"
MISSING = "missing"          # 404: gone, renamed, or private to this token
FORBIDDEN = "forbidden"      # 403/401: seen but refused (SAML, rate limit, scope)
UNKNOWN = "unknown"          # the check itself could not run

# Statuses that justify refusing an add or offering a removal. UNKNOWN never
# does: a network blip must not delete a repo or block a legitimate one.
ACTIONABLE = (MISSING, FORBIDDEN)

MAX_PARALLEL_CHECKS = 8

_HTTP_CODE = re.compile(r"HTTP (\d{3})")


def check_repo(owner: str, repo: str, gh=None) -> dict:
    """{"status", "detail"} for one repo. Never raises.

    gh is resolved at call time, not import time, so patching src.gh.run_gh
    reaches this the same way it reaches autoreview_config.list_repos.
    """
    if gh is None:
        from src.gh import run_gh as gh
    try:
        data = gh(["api", f"repos/{owner}/{repo}", "--jq", ".full_name"])
    except (RuntimeError, OSError) as e:
        message = str(e)
        code = _HTTP_CODE.search(message)
        code = code.group(1) if code else ""
        if code == "404":
            return {"status": MISSING,
                    "detail": "not found — deleted, renamed, or not visible "
                              "to this GitHub token"}
        if code in ("401", "403"):
            return {"status": FORBIDDEN,
                    "detail": f"access refused (HTTP {code}) — check token "
                              f"scopes or SAML authorisation"}
        return {"status": UNKNOWN, "detail": message[:200]}
    if not data:
        return {"status": UNKNOWN, "detail": "empty response from gh"}
    return {"status": OK, "detail": str(data)}


def check_repos(pairs: list[tuple[str, str]], gh=None) -> dict:
    """Check many repos at once. Keyed by "owner/repo"; never raises."""
    if not pairs:
        return {}
    workers = min(MAX_PARALLEL_CHECKS, len(pairs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: check_repo(p[0], p[1], gh), pairs))
    return {f"{o}/{r}": result for (o, r), result in zip(pairs, results)}
