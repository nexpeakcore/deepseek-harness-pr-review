"""Phase 1: fetch PR metadata, files, commits, review threads from GitHub."""
import json
import sys
from pathlib import Path

from src.gh import run_gh as _default_gh


ISSUE_BODY_MAX = 4000


def _pr_graphql(owner: str, repo: str, n: int, gh) -> tuple[list[dict], list[dict]]:
    """One round trip for review threads + linked issues.

    Linked issues are an intent signal: when a PR ships with no usable
    description, the issue it closes is usually the only written statement of
    what the change is supposed to do (see src/claims.py).
    """
    query = """
    query($owner:String!,$repo:String!,$pr:Int!){
      repository(owner:$owner,name:$repo){
        pullRequest(number:$pr){
          closingIssuesReferences(first:10){
            nodes{ number title body }
          }
          reviewThreads(first:100){
            nodes{
              isResolved
              isOutdated
              comments(first:100){
                nodes{
                  path
                  line
                  author{login}
                  body
                }
              }
            }
          }
        }
      }
    }
    """
    payload = gh(["api", "graphql", "-f", f"query={query}",
                  "-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"pr={n}"])
    if not isinstance(payload, dict):
        raise RuntimeError(f"graphql failed: {payload}")
    if "errors" in payload:
        raise RuntimeError(f"graphql errors: {payload['errors']}")
    if "data" not in payload:
        raise RuntimeError(f"graphql failed: {payload}")
    pr = (payload["data"] or {}).get("repository", {}).get("pullRequest")
    if pr is None:
        raise RuntimeError(f"graphql: pullRequest #{n} not found (owner={owner}, repo={repo})")

    nodes = (pr.get("reviewThreads") or {}).get("nodes") or []
    threads = []
    truncated = len(nodes) == 100
    for node in nodes:
        comments = (node.get("comments") or {}).get("nodes") or []
        if len(comments) == 100:
            truncated = True
        for c in comments:
            threads.append({
                "path": c.get("path"),
                "line": c.get("line"),
                "author": (c.get("author") or {}).get("login"),
                "body": c.get("body"),
                "resolved": node["isResolved"],
                "outdated": node["isOutdated"],
            })
    if truncated:
        print("[snapshot] warning: review threads truncated at 100 — snapshot data incomplete", file=sys.stderr)

    issues = []
    for node in (pr.get("closingIssuesReferences") or {}).get("nodes") or []:
        body = node.get("body") or ""
        issues.append({
            "number": node.get("number"),
            "title": node.get("title") or "",
            "body": body[:ISSUE_BODY_MAX],
        })
    return threads, issues


def build_snapshot(owner: str, repo: str, n: int, session_dir: Path,
                   gh=_default_gh) -> dict:
    """Fetch PR data and save snapshot.json into session_dir. Returns the snapshot dict."""
    meta = gh([f"api", f"repos/{owner}/{repo}/pulls/{n}"])
    files = gh([f"api", f"repos/{owner}/{repo}/pulls/{n}/files", "--paginate"])
    commits = gh([f"api", f"repos/{owner}/{repo}/pulls/{n}/commits", "--paginate"])
    threads, linked_issues = _pr_graphql(owner, repo, n, gh)

    snapshot = {
        "owner": owner,
        "repo": repo,
        "pr": n,
        "title": meta.get("title", ""),
        "body": meta.get("body") or "",
        "author": (meta.get("user") or {}).get("login", ""),
        "base": (meta.get("base") or {}).get("ref", ""),
        "head": (meta.get("head") or {}).get("ref", ""),
        "head_sha": (meta.get("head") or {}).get("sha", ""),
        "labels": [l.get("name") for l in meta.get("labels", [])],
        "files": [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch", ""),
            }
            for f in files
        ],
        "commits": [
            {"sha": c.get("sha", ""), "message": c.get("commit", {}).get("message", "")}
            for c in commits
        ],
        "threads": threads,
        "linked_issues": linked_issues,
    }

    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "snapshot.json").write_text(json.dumps(snapshot, indent=2))
    return snapshot
