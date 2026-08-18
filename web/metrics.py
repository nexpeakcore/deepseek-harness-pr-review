"""Read sessions/ data into PR and repo metric records. Pure logic, no HTTP."""
import json
import sys
from datetime import datetime
from pathlib import Path

from src.synthesize import _overall_verdict

VERDICTS = ("ACCURATE", "PARTIAL", "MISLEADING", "NO_CLAIMS")
REQUIRED_FILES = ("snapshot.json", "findings.json")


def _warn(msg: str) -> None:
    print(f"[metrics] {msg}", file=sys.stderr)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        _warn(f"skipping corrupt file: {path}")
        return None


def _read_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        _warn(f"skipping corrupt file: {path}")
        return []


def _verdict_key(verdict: str) -> str:
    """Normalize verdict names to safe dict keys (NO CLAIMS -> NO_CLAIMS)."""
    return verdict.replace(" ", "_")


def _session_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        d for d in root.glob("*/*/pr-*")
        if d.is_dir() and all((d / f).exists() for f in REQUIRED_FILES))


def _session_updated(session_dir: Path) -> str:
    try:
        return datetime.fromtimestamp(session_dir.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M")
    except OSError:
        return ""


def list_repos(session_root: Path) -> list[tuple[str, str]]:
    """Return sorted [(owner, repo)] pairs with review data."""
    pairs = sorted({(d.parent.parent.name, d.parent.name)
                    for d in _session_dirs(session_root)})
    return pairs


def _read_rounds(session_dir: Path) -> int:
    """Rounds from rounds.txt; fallback 1 for legacy sessions; garbage → 1."""
    path = session_dir / "rounds.txt"
    if not path.exists():
        return 1
    try:
        return max(1, int(path.read_text().strip()))
    except (OSError, ValueError):
        return 1


def pr_record(session_root: Path, owner: str, repo: str, n: int) -> dict | None:
    """Build one PR metric record. None if missing/corrupt data."""
    session_dir = session_root / owner / repo / f"pr-{n}"
    if not session_dir.is_dir():
        return None
    snapshot = _read_json(session_dir / "snapshot.json")
    findings = _read_json(session_dir / "findings.json")
    if snapshot is None or findings is None:
        return None

    answers = _read_json_list(session_dir / "answers.json")

    report = session_dir / "report.md"
    failed = report.exists() and report.read_text(
        errors="replace").startswith("# Review FAILED")

    claims = findings.get("claims", [])
    docs = findings.get("docs", [])
    impact = findings.get("impact", [])

    return {
        "pr": n,
        "title": snapshot.get("title", ""),
        "author": snapshot.get("author", ""),
        "base": snapshot.get("base", ""),
        "head": snapshot.get("head", ""),
        "verdict": _verdict_key(_overall_verdict(findings)),
        "claims_total": len(claims),
        "bugs": sum(1 for c in claims
                    if c.get("status") in ("FAIL", "PARTIAL"))
                + sum(1 for i in impact
                      if i.get("impact") in ("BROKEN", "RISK")),
        "bug_breakdown": {
            "claims_fail": sum(1 for c in claims if c.get("status") == "FAIL"),
            "claims_partial": sum(1 for c in claims
                                  if c.get("status") == "PARTIAL"),
            "impact_broken": sum(1 for i in impact
                                 if i.get("impact") == "BROKEN"),
            "impact_risk": sum(1 for i in impact if i.get("impact") == "RISK"),
        },
        "doc_errors": sum(1 for d in docs
                          if d.get("status") in ("WRONG", "FABRICATED", "STALE")),
        "doc_breakdown": {
            "wrong": sum(1 for d in docs if d.get("status") == "WRONG"),
            "fabricated": sum(1 for d in docs
                              if d.get("status") == "FABRICATED"),
            "stale": sum(1 for d in docs if d.get("status") == "STALE"),
        },
        "open_questions": sum(1 for a in answers
                              if a.get("answer") in ("SKIPPED", "")),
        "rounds": _read_rounds(session_dir),
        "updated_at": _session_updated(session_dir),
        "failed": failed,
    }


def repo_record(session_root: Path, owner: str, repo: str) -> dict | None:
    """Build one repo aggregate record. None if repo has no review data."""
    dirs = [d for d in _session_dirs(session_root)
            if d.parent.parent.name == owner and d.parent.name == repo]
    if not dirs:
        return None
    prs = []
    for d in dirs:
        n = int(d.name.split("-")[1])
        rec = pr_record(session_root, owner, repo, n)
        if rec is not None:
            prs.append(rec)
    prs.sort(key=lambda r: r["updated_at"], reverse=True)
    verdict_count = {v: 0 for v in VERDICTS}
    for r in prs:
        if not r["failed"]:
            verdict_count[r["verdict"]] += 1
    return {
        "owner": owner,
        "repo": repo,
        "prs_total": len(prs),
        "bugs_total": sum(r["bugs"] for r in prs),
        "doc_errors_total": sum(r["doc_errors"] for r in prs),
        "verdict_count": verdict_count,
        "prs": prs,
    }


def pr_detail(session_root: Path, owner: str, repo: str, n: int) -> dict | None:
    """Full data for the PR detail page (claims merged with claim text)."""
    rec = pr_record(session_root, owner, repo, n)
    if rec is None:
        return None
    session_dir = session_root / owner / repo / f"pr-{n}"
    snapshot = _read_json(session_dir / "snapshot.json")
    findings = _read_json(session_dir / "findings.json")
    claims_json = _read_json_list(session_dir / "claims.json")
    by_id = {c.get("id"): c for c in claims_json if isinstance(c, dict)}

    claims = []
    for fc in findings.get("claims", []):
        base = by_id.get(fc.get("id"), {})
        claims.append({
            "id": fc.get("id", ""),
            "text": base.get("text", ""),
            "category": base.get("category", ""),
            "status": fc.get("status", ""),
            "evidence": fc.get("evidence", []),
            "note": fc.get("note", ""),
        })

    answers = _read_json_list(session_dir / "answers.json")

    return {
        "pr": rec,
        "title": snapshot.get("title", ""),
        "body": snapshot.get("body", ""),
        "claims": claims,
        "docs": findings.get("docs", []),
        "impact": findings.get("impact", []),
        "threads": findings.get("threads", []),
        "answers": answers,
    }


def open_prs(session_root: Path, owner: str, repo: str, gh=None) -> list[dict]:
    """Merge GitHub open PRs with session state.

    Returns rows: {pr, title, draft, status, rounds, bugs, doc_errors,
    unavailable} sorted by pr desc.
    status: reviewed | reviewing | failed | not_reviewed.
    gh failure → rows for reviewed sessions only, unavailable=True.
    """
    if gh is None:
        from src.gh import run_gh
        gh = run_gh
    rows = []
    unavailable = False
    try:
        prs = gh(["api", f"repos/{owner}/{repo}/pulls?state=open", "--paginate"])
    except (RuntimeError, OSError):
        prs = []
        unavailable = True

    session_dir = session_root / owner / repo
    seen = set()
    for p in prs:
        n = int(p["number"])
        seen.add(n)
        d = session_dir / f"pr-{n}"
        info = review_process_info(d)
        if info:  # lock sống → đang review/re-review (kể cả khi đã có findings)
            rec = pr_record(session_root, owner, repo, n) if (d / "findings.json").exists() else None
            rows.append({
                "pr": n, "title": p.get("title", ""),
                "draft": bool(p.get("draft")),
                "status": "reviewing", "rounds": None,
                "pid": info["pid"],
                "started_at": info["started_at"],
                "bugs": rec["bugs"] if rec else None,
                "doc_errors": rec["doc_errors"] if rec else None,
                "unavailable": unavailable,
            })
        elif (d / "findings.json").exists():
            rec = pr_record(session_root, owner, repo, n)
            rows.append({
                "pr": n, "title": p.get("title", ""),
                "draft": bool(p.get("draft")),
                "status": "reviewed",
                "rounds": _read_rounds(d),
                "bugs": rec["bugs"] if rec else 0,
                "doc_errors": rec["doc_errors"] if rec else 0,
                "unavailable": unavailable,
            })
        elif d.exists():
            # Session tồn tại nhưng không có findings và không có review đang
            # chạy → review bị gián đoạn (crash/SIGKILL/treo) → đánh dấu failed
            # thay vì "reviewing" vĩnh viễn.
            rows.append({
                "pr": n, "title": p.get("title", ""),
                "draft": bool(p.get("draft")),
                "status": "failed", "rounds": None,
                "bugs": None, "doc_errors": None,
                "unavailable": unavailable,
            })
        else:
            rows.append({
                "pr": n, "title": p.get("title", ""),
                "draft": bool(p.get("draft")),
                "status": "not_reviewed", "rounds": None,
                "bugs": None, "doc_errors": None,
                "unavailable": unavailable,
            })

    if unavailable:
        # gh lỗi → fallback: PR đã review từ sessions
        for d in sorted((session_root / owner / repo).glob("pr-*")):
            n = int(d.name.split("-")[1])
            if n in seen:
                continue
            if (d / "findings.json").exists():
                rec = pr_record(session_root, owner, repo, n)
                rows.append({
                    "pr": n, "title": "", "draft": False,
                    "status": "reviewed",
                    "rounds": _read_rounds(d),
                    "bugs": rec["bugs"] if rec else 0,
                    "doc_errors": rec["doc_errors"] if rec else 0,
                    "unavailable": True,
                })

    rows.sort(key=lambda r: r["pr"], reverse=True)
    return rows


def review_process_info(session_dir: Path) -> dict | None:
    """PID + started_at of a running review, or None if not running/stale.

    Reads session_dir/review.lock (JSON {pid, started_at}); lock without a
    live PID is treated as stale (not running).
    """
    lock = session_dir / "review.lock"
    if not lock.exists():
        return None
    try:
        import json as _json

        meta = _json.loads(lock.read_text())
        pid = int(meta.get("pid", 0))
        started_at = meta.get("started_at", "")
    except (ValueError, OSError, _json.JSONDecodeError):
        return None
    if pid <= 0:
        return None
    try:
        import os as _os

        _os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return {"pid": pid, "started_at": started_at}
