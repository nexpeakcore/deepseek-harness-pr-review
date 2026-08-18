"""Phase 2: turn the PR into verifiable claims (LLM).

Two modes. When the PR has a usable description, claims are *stated*: each
sentence of the description becomes a claim, and phase 3 checks it against the
code — description is the hypothesis, code is the evidence.

When the description is missing or boilerplate, that pipeline has nothing to
chew on and the review goes silent exactly on the PRs that deserve it most. So
we invert it: claims are *inferred* from the change itself (commits, branch
name, labels, linked issues, diff), and phase 3 checks the code against its own
implied intent — scope creep, untested behaviour changes, code that contradicts
what its own names promise.
"""
import json
import re
from pathlib import Path

from src.llm import chat as _default_chat

CATEGORIES = ("feature", "bugfix", "refactor", "perf", "ux", "docs")
SOURCES = ("stated", "inferred")

# A description below this survives boilerplate stripping but still says
# nothing verifiable ("fix", "update", "wip", "#123").
MIN_DESCRIPTION_CHARS = 30
MIN_DESCRIPTION_WORDS = 4

# Diff budget for the inferred pass — enough to see intent, bounded so a large
# PR cannot blow the context window.
MAX_DIFF_FILES = 40
MAX_PATCH_CHARS = 2000
MAX_DIFF_TOTAL = 60_000

SCHEMA_HINT = """
Split the PR description below into verifiable claims (each claim must be
verifiable by reading the code). Return a JSON array matching this schema:
[{"id": "C1", "text": "<brief>", "category": "feature|bugfix|refactor|perf|ux|docs",
  "files": ["<related files, empty if unknown>"], "docs": ["<docs this claim mentions>"]}]
Do not add any text outside the JSON. If there are no claims, return [].
"""

INFERRED_HINT = """
This PR has no usable description. Reconstruct what the change is supposed to
do from the evidence below (commit messages, branch name, labels, linked
issues, and the diff), and state it as verifiable claims — the description the
author should have written.

Rules:
- Each claim must be checkable by reading the code. Describe observable
  behaviour ("retries failed payments up to 5 times"), not diff mechanics
  ("edits payment.py").
- Cover every distinct intent you can see, including changes that look
  unrelated to the main one — an unrelated change is exactly what a reviewer
  needs flagged.
- Do not invent motivation you cannot see in the evidence. If a hunk's purpose
  is unclear, still emit a claim describing what it does and say it is unclear
  in the text.

Return a JSON array matching this schema, nothing else:
[{"id": "C1", "text": "<brief>", "category": "feature|bugfix|refactor|perf|ux|docs",
  "files": ["<files this claim covers>"], "docs": ["<docs this claim touches>"]}]
If the diff is empty, return [].
"""

# Stripped before judging whether a description says anything: HTML comments
# (PR template guidance), headings, list bullets, checkboxes, rules, images.
_BOILERPLATE = [
    re.compile(r"<!--.*?-->", re.DOTALL),
    re.compile(r"^\s{0,3}#{1,6}\s.*$", re.MULTILINE),
    re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s*", re.MULTILINE),
    re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE),
    re.compile(r"!\[[^\]]*\]\([^)]*\)"),
]
# Lines that are only a bare issue/PR reference or a URL carry no claim.
_REFERENCE_ONLY = re.compile(
    r"^\s*(closes|fixes|resolves|refs?|see)?\s*[:#]?\s*"
    r"(#\d+|https?://\S+)\s*$", re.IGNORECASE)


def strip_boilerplate(body: str) -> str:
    """The prose left after PR-template scaffolding is removed."""
    text = body or ""
    for pattern in _BOILERPLATE:
        text = pattern.sub(" ", text)
    kept = [ln for ln in text.splitlines()
            if ln.strip() and not _REFERENCE_ONLY.match(ln)]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def description_is_thin(snapshot: dict) -> bool:
    """True when the body is missing, boilerplate-only, or too short to verify."""
    prose = strip_boilerplate(snapshot.get("body") or "")
    return (len(prose) < MIN_DESCRIPTION_CHARS
            or len(prose.split()) < MIN_DESCRIPTION_WORDS)


def intent_signals(snapshot: dict) -> str:
    """Everything about the author's intent that is not the description.

    Commit messages, branch name, labels and the linked issue are usually more
    honest than a PR body anyway; they are the only intent we have when the
    body is empty.
    """
    lines = [f"Title: {snapshot.get('title', '')}",
             f"Branch: {snapshot.get('head', '')} -> {snapshot.get('base', '')}"]
    labels = [l for l in snapshot.get("labels") or [] if l]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")

    commits = snapshot.get("commits") or []
    if commits:
        lines.append("Commit messages:")
        lines += [f"- {(c.get('message') or '').strip().splitlines()[0]}"
                  for c in commits if (c.get("message") or "").strip()]

    for issue in snapshot.get("linked_issues") or []:
        lines.append(f"Linked issue #{issue.get('number')}: {issue.get('title', '')}")
        body = (issue.get("body") or "").strip()
        if body:
            lines.append(body)

    threads = snapshot.get("threads") or []
    if threads:
        lines.append("Review comments so far:")
        lines += [f"- {t.get('author')}: {(t.get('body') or '')[:200]}"
                  for t in threads[:20]]
    return "\n".join(lines)


def diff_digest(snapshot: dict) -> str:
    """Bounded rendering of the diff — the only statement of intent we can trust."""
    files = snapshot.get("files") or []
    parts, total = [], 0
    # Tests first: a test diff describes expected behaviour more honestly than
    # any prose, so it must survive truncation.
    ordered = sorted(files, key=lambda f: 0 if _is_test(f.get("filename", "")) else 1)
    for f in ordered[:MAX_DIFF_FILES]:
        header = (f"--- {f.get('filename', '')} "
                  f"({f.get('status', '')} +{f.get('additions', 0)}/-{f.get('deletions', 0)})")
        patch = (f.get("patch") or "")[:MAX_PATCH_CHARS]
        chunk = f"{header}\n{patch}" if patch else header
        if total + len(chunk) > MAX_DIFF_TOTAL:
            parts.append(f"(diff truncated: {len(files) - len(parts)} more files)")
            break
        parts.append(chunk)
        total += len(chunk)
    if len(files) > MAX_DIFF_FILES:
        parts.append(f"(… {len(files) - MAX_DIFF_FILES} more files not shown)")
    return "\n".join(parts) or "(no files changed)"


def _is_test(filename: str) -> bool:
    low = filename.lower()
    return ("test" in low or "spec" in low) and not low.endswith(".md")


def _parse_claims(raw: str, source: str) -> list[dict]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("claims response must be a list")
    for c in data:
        if not isinstance(c, dict) or "id" not in c:
            raise ValueError(f"claim has invalid schema (missing id): {c}")
        if "text" not in c or "category" not in c:
            raise ValueError(f"claim has invalid schema (missing text/category): {c}")
        if c["category"] not in CATEGORIES:
            raise ValueError(f"invalid category: {c.get('category')}")
        # Stamped here, not asked of the model: provenance is ours to know.
        c["source"] = source
    return data


def _run_pass(system: str, user: str, cfg: dict, source: str, chat) -> list[dict]:
    try:
        raw = chat([{"role": "system", "content": system},
                    {"role": "user", "content": user}],
                   model=cfg["model"], api_key=cfg["api_key"],
                   base_url=cfg["base_url"])
        return _parse_claims(raw, source)
    except (json.JSONDecodeError, ValueError, RuntimeError) as e:
        raise RuntimeError(f"invalid claims response ({source}): {e}") from e


def extract_claims(snapshot: dict, cfg: dict, session_dir: Path,
                   chat=_default_chat) -> list[dict]:
    """Extract claims from the snapshot. Save claims.json, return the claims list.

    Falls back to inferred claims when the description is thin, or when the
    stated pass finds nothing verifiable in it.
    """
    claims: list[dict] = []
    if not description_is_thin(snapshot):
        description = f"# Title: {snapshot['title']}\n\n{snapshot['body']}"
        file_names = [f["filename"] for f in snapshot.get("files", [])]
        claims = _run_pass(
            SCHEMA_HINT,
            f"Description:\n{description}\n\nFiles changed:\n{file_names}",
            cfg, "stated", chat)

    if not claims:
        claims = _run_pass(
            INFERRED_HINT,
            f"Evidence of intent:\n{intent_signals(snapshot)}\n\n"
            f"Diff:\n{diff_digest(snapshot)}",
            cfg, "inferred", chat)

    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "claims.json").write_text(json.dumps(claims, indent=2))
    return claims


def all_inferred(claims: list[dict] | None) -> bool:
    """True when every claim was reconstructed from code, not read from a description."""
    return bool(claims) and all(c.get("source") == "inferred" for c in claims)
