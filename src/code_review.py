"""The code axis: does the change itself hold up?

Every other axis measures the PR against something outside the code — its
description, its docs, its requirements. None of them reads the diff asking
whether it is *correct*, so a PR whose description is accurate and whose code
has a race in it came back ACCURATE, green. This axis asks that question.

It runs in two steps, because it posts on the lines of someone's PR:

1. Code agents read the diff (sharded by file, like claims are by count) and
   report defects. Each must carry a concrete failure scenario — the input or
   state, and the wrong result — or it is not reported at all. That one rule is
   what separates a defect from a style opinion.
2. One verify agent re-reads every BLOCKER and MAJOR adversarially and confirms
   or rejects it. A false positive posted on a line is the fastest way to teach
   a team to ignore the tool, so only confirmed issues are posted inline;
   MINOR ones and unconfirmed ones stay in the report.
"""
import hashlib
import json
import os
import re
import tempfile

from src.gh import run_gh

SEVERITIES = ("BLOCKER", "MAJOR", "MINOR")
CATEGORIES = ("correctness", "security", "concurrency", "error-handling",
              "resource", "performance", "test-gap")
# Severities worth a second agent's time and a comment on the line.
VERIFIED_SEVERITIES = ("BLOCKER", "MAJOR")

# One code agent per this many files, or per this much patch text, whichever
# comes first — past it, an agent trades depth per file for coverage.
CODE_SHARD_FILES = 20
CODE_SHARD_PATCH_CHARS = 120_000

CODE_SCHEMA = """{
  "code": [{"file": "path/in/repo.py", "line": 42,
            "severity": "BLOCKER|MAJOR|MINOR",
            "category": "correctness|security|concurrency|error-handling|resource|performance|test-gap",
            "title": "one-line statement of the defect",
            "scenario": "the concrete input or state, and the wrong result it produces",
            "evidence": ["file:line"]}],
  "unresolved_questions": ["question ≤20 words for the human"]
}"""

VERDICTS_SCHEMA = """{
  "verdicts": [{"id": "K1", "verdict": "CONFIRMED|REJECTED", "reason": "brief"}]
}"""

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _walk_patch(patch: str):
    """Yield (kind, new_line, text) for every line of a unified-diff patch.

    kind is "+", "-", " " or "@"; new_line is the line number in the new
    version of the file, None for removed lines and hunk headers.
    """
    new_line = None
    for raw in (patch or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
            yield "@", None, raw
            continue
        if new_line is None:
            continue
        if raw.startswith("-"):
            yield "-", None, raw
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            yield (raw[:1] or " "), new_line, raw
            new_line += 1


def commentable_lines(patch: str) -> set[int]:
    """New-side lines GitHub accepts an inline comment on: those in a hunk."""
    return {n for kind, n, _ in _walk_patch(patch) if kind in ("+", " ")}


def annotate_patch(patch: str) -> str:
    """The patch with each line's new-file line number in front of it.

    An agent working from hunk headers alone miscounts, and a wrong line lands
    the inline comment on the wrong code — or GitHub rejects the review.
    """
    out = []
    for kind, n, text in _walk_patch(patch):
        out.append(text if kind == "@" else f"{'' if n is None else n:>6} {text}")
    return "\n".join(out)


def is_patchless(f: dict) -> bool:
    """A text change GitHub sent without a patch, because the diff is too large.

    Binary files come without a patch too, but with zero line counts. The head
    version of a patchless text file does not say which lines changed, so no
    agent can review the change — and a shard holding one must not read as
    clean. These are recorded and make the code verdict partial.
    """
    # A deletion counts too: removing code can break every caller of it.
    return (not f.get("patch")
            and (f.get("additions") or 0) + (f.get("deletions") or 0) > 0)


def render_diff(files: list[dict]) -> str:
    """The diff an agent reads, one file after another, line-numbered."""
    parts = []
    for f in files:
        header = (f"=== {f.get('filename', '')} ({f.get('status', '')} "
                  f"+{f.get('additions', 0)}/-{f.get('deletions', 0)})")
        patch = f.get("patch") or ""
        if patch:
            body = annotate_patch(patch)
        elif is_patchless(f):
            body = ("(GitHub sent no patch — the diff is too large. This file is "
                    "recorded as not reviewed; do not report on it.)")
        else:
            body = "(GitHub sent no patch — a binary file, or no line changes.)"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts) + "\n"


def shard_files(files: list[dict]) -> list[list[dict]]:
    """Group changed files into agent-sized shards, keeping the PR's order."""
    shards, current, size = [], [], 0
    for f in files:
        patch_len = len(f.get("patch") or "")
        if current and (len(current) >= CODE_SHARD_FILES
                        or size + patch_len > CODE_SHARD_PATCH_CHARS):
            shards.append(current)
            current, size = [], 0
        current.append(f)
        size += patch_len
    if current:
        shards.append(current)
    return shards


def build_code_prompt(pr_context: str, diff_file: str,
                      security_block: str, write_instruction: str,
                      shard: tuple[int, int] | None = None) -> str:
    scope = ""
    if shard:
        scope = (f"\nYou own batch {shard[0]} of {shard[1]} of the changed files. "
                 f"Other agents review the rest — report only on yours.\n")
    return f"""
You are in the workspace containing the PR code, checked out at the PR head.
Task: review the CHANGED CODE for defects. This is your only job — other agents
check the description, the docs and requirement impact. Do not report on those.

{pr_context}
{scope}
The diff for the files you own is in {diff_file} — read it first. Every line
carries its line number in the NEW version of the file; removed lines have
none. A hunk alone is not enough to judge: read the surrounding code, the
callers of anything whose behaviour changed, and the tests.

Look for:
- correctness: wrong result, unhandled edge case (empty, None, zero, boundary,
  encoding, timezone), off-by-one, inverted or incomplete condition
- error-handling: exception swallowed or too broad, failure path that leaves
  state half-written, error reported as success
- security: untrusted input reaching a shell, SQL, file path, template,
  deserializer or URL; missing authorization; secret in code or logs
- concurrency: race, lock misuse, shared mutable state
- resource: leaked file, connection or process; unbounded growth
- performance: accidental quadratic or N+1 at a realistic input size
- test-gap: a changed behaviour no test exercises, where the gap could hide a
  regression

Rules:
1. Every issue needs a concrete scenario: the input or state, and the wrong
   result. If you cannot write one, it is not an issue — leave it out.
2. "line" is a line number from the NEW version, on the line where the defect
   is. Prefer a changed line; point at unchanged code only when the change
   makes it newly wrong.
3. Severity: BLOCKER = breaks in normal use, loses or corrupts data, or opens
   a security hole. MAJOR = breaks on a realistic edge case or failure path.
   MINOR = real, but low impact.
4. No style, naming, formatting or "consider" suggestions.
5. Report each defect once. An empty list is a valid answer — do not invent
   issues to fill it.
{security_block}{write_instruction}"""


def build_verify_prompt(pr_context: str, issues: list[dict],
                        diff_files: list[str], security_block: str,
                        write_instruction: str) -> str:
    listed = json.dumps([{k: i.get(k) for k in
                          ("id", "file", "line", "severity", "category",
                           "title", "scenario")} for i in issues], indent=2)
    diffs = ", ".join(diff_files) or "(not available)"
    return f"""
You are in the workspace containing the PR code, checked out at the PR head.
Task: adversarially check defects another reviewer reported in this PR. You
are the last check before these are posted as comments on the author's lines,
so a wrong one costs the author's trust in every later one.

{pr_context}

The change itself is in {diffs} — every line numbered in the NEW file,
removed lines unnumbered. Read the part each defect touches.

Reported defects:
{listed}

For each id, read the code and trace the scenario:
- CONFIRMED — the scenario really happens with the code as written, and this
  change caused it: it introduced the code, or made unchanged code newly
  reachable or newly wrong.
- REJECTED — it cannot happen (guarded elsewhere, a caller never passes that
  value, a type or check rules it out, the code was misread), it is not a
  defect at all, or it predates this PR: the code involved is unchanged and
  the change does not make it newly wrong. That may be a real bug, but it is
  not this PR's, and it will not be posted on this PR's lines.

When the scenario depends on something you cannot find in the code, REJECT
it. Report every id exactly once.
{security_block}{write_instruction}"""


def normalize_issues(raw: list) -> list[dict]:
    """Well-formed issues, numbered K1.. in order. Malformed ones are dropped.

    Shards number independently, so ids are assigned here, after the merge.
    """
    issues = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).upper()
        if severity not in SEVERITIES or not item.get("file") or not item.get("title"):
            continue
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        scenario = str(item.get("scenario") or "").strip()
        if not scenario:
            continue  # no scenario, no issue — held here, not only in the prompt
        # One reference given as a bare string is one reference, not a list of
        # characters; anything else malformed must not cost the review a crash.
        evidence = item.get("evidence")
        if isinstance(evidence, str):
            evidence = [evidence]
        elif not isinstance(evidence, list):
            evidence = []
        category = str(item.get("category", "")).lower()
        path = str(item["file"])
        issues.append({
            "file": path[2:] if path.startswith("./") else path,
            "line": line,
            "severity": severity,
            "category": category if category in CATEGORIES else "correctness",
            "title": str(item["title"]),
            "scenario": scenario,
            "evidence": [str(e) for e in evidence],
        })
    for n, issue in enumerate(issues, start=1):
        issue["id"] = f"K{n}"
    return issues


def needs_verification(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["severity"] in VERIFIED_SEVERITIES]


def by_severity(issues: list[dict]) -> list[dict]:
    """Worst first — the order a reader should meet them in."""
    rank = {s: n for n, s in enumerate(SEVERITIES)}
    return sorted(issues, key=lambda i: rank.get(i.get("severity"), len(rank)))


def apply_verdicts(issues: list[dict],
                   verdicts: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """(kept, rejected). verdicts=None means the verify step did not run.

    Each kept issue gets `verified`: True (confirmed), None (not checked —
    MINOR, verifier failed, or the verifier skipped the id). A REJECTED issue
    is moved out, with the reason, so precision can be measured later rather
    than the evidence being thrown away.
    """
    by_id = {}
    for v in verdicts or []:
        if isinstance(v, dict) and v.get("id"):
            by_id[v["id"]] = v
    kept, rejected = [], []
    for issue in issues:
        v = by_id.get(issue["id"])
        verdict = str((v or {}).get("verdict", "")).upper()
        if issue["severity"] in VERIFIED_SEVERITIES and verdict == "REJECTED":
            rejected.append({**issue, "reason": (v or {}).get("reason", "")})
            continue
        confirmed = (issue["severity"] in VERIFIED_SEVERITIES
                     and verdict == "CONFIRMED")
        kept.append({**issue, "verified": True if confirmed else None})
    return kept, rejected


# --- verdict ---------------------------------------------------------------

def reviewed(findings: dict) -> bool:
    """Did the code axis produce a result for this session at all?"""
    if not isinstance(findings.get("code"), list):
        return False  # session from before the code axis, or axis disabled
    meta = findings.get("code_meta") or {}
    shards = meta.get("shards", 0)
    return not (shards and meta.get("failed_shards", 0) >= shards)


def code_counts(findings: dict) -> dict:
    issues = findings.get("code") if reviewed(findings) else []
    counts = {s.lower(): sum(1 for i in issues if i.get("severity") == s)
              for s in SEVERITIES}
    counts["by_category"] = {}
    for i in issues:
        cat = i.get("category", "correctness")
        counts["by_category"][cat] = counts["by_category"].get(cat, 0) + 1
    return counts


def code_verdict(findings: dict) -> str:
    """NOT_RUN / BLOCKER / MAJOR / CLEAN — the worst severity that survived."""
    if not reviewed(findings):
        return "NOT_RUN"
    c = code_counts(findings)
    if c["blocker"]:
        return "BLOCKER"
    if c["major"]:
        return "MAJOR"
    return "CLEAN"


def code_verdict_label(findings: dict) -> str:
    verdict = code_verdict(findings)
    if verdict == "NOT_RUN":
        return "Code: not reviewed"
    c = code_counts(findings)
    parts = [f"{c[k]} {k}" for k in ("blocker", "major", "minor") if c[k]]
    if verdict == "CLEAN":
        label = ("Code: no issues found" if not c["minor"] else
                 f"Code: no blocking issues ({c['minor']} minor)")
    else:
        label = "Code: " + " · ".join(parts)
    meta = findings.get("code_meta") or {}
    if meta.get("failed_shards") or meta.get("patchless_files"):
        label += " (partial)"
    return label


# --- inline comments -------------------------------------------------------

# key, then — since comments carry it — a digest of the code on the flagged line.
INLINE_MARKER_RE = re.compile(r"<!-- harness-code:([0-9a-f]{12})(?::([0-9a-f]{8}))? -->")
REVIEW_MARKER = "<!-- harness-code-review -->"
SEVERITY_ICON = {"BLOCKER": "🔴", "MAJOR": "🟠", "MINOR": "⚪"}


def issue_key(issue: dict) -> str:
    """Identity of an issue across rounds, for de-duplication.

    Deliberately not the line: an unrelated push above it moves the line, and
    the same defect would be posted again one line lower.
    """
    title = re.sub(r"\W+", " ", issue.get("title", "").lower()).strip()
    raw = f"{issue.get('file', '')}|{issue.get('category', '')}|{title}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def line_anchor(text: str) -> str:
    """A digest of the code on a flagged line, blind to whitespace.

    A push above an issue changes its line number, not the code on it, so the
    code is what ties this round's issue to last round's comment. The key
    alone cannot: the same pattern at two lines shares a key, and pairing by
    position — then by count — handed an old comment to a new issue.
    """
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:8]


def new_side_text(patch: str) -> dict[int, str]:
    """{new-file line: code} for every line GitHub accepts a comment on."""
    return {n: text[1:] for kind, n, text in _walk_patch(patch) if kind in ("+", " ")}


def inline_body(issue: dict, anchor: str) -> str:
    icon = SEVERITY_ICON.get(issue["severity"], "")
    lines = [f"{icon} **{issue['severity']} · {issue['category']}** — {issue['title']}"]
    if issue.get("scenario"):
        lines += ["", f"**When:** {issue['scenario']}"]
    lines += ["", "<sub>Harness code review · confirmed by a second agent</sub>",
              f"<!-- harness-code:{issue_key(issue)}:{anchor} -->"]
    return "\n".join(lines)


def plan_inline(issues: list[dict], files: list[dict],
                existing: list[dict]) -> tuple[list[dict], dict]:
    """The inline comments to post this round, and why the others were not.

    Posted: confirmed BLOCKER/MAJOR issues on a line inside the diff that this
    tool has not already commented on. Anything else stays in the report —
    GitHub rejects a whole review over one comment outside the diff.

    "Already commented on" means, in order: this tool's comment sits on the
    issue's line; or an earlier comment carries the issue's key and the digest
    of the code on its line — the defect moved, and its code moved with it.
    Comments from before digests existed carry the key alone, and each absorbs
    one moved issue by count.
    """
    texts = {f.get("filename"): new_side_text(f.get("patch") or "") for f in files}
    at_spot, anchored, legacy = {}, {}, {}
    for c in existing:
        m = INLINE_MARKER_RE.search(c.get("body") or "")
        if not m:
            continue
        key, anchor = m.group(1), m.group(2)
        spot = (c.get("path"), c.get("line") or c.get("original_line"))
        at_spot[spot] = (key, anchor)
        if anchor:
            anchored[key, anchor] = anchored.get((key, anchor), 0) + 1
        else:
            legacy.setdefault(key, set()).add(spot)
    eligible = []
    skipped = {"unconfirmed": 0, "outside_diff": 0, "already_posted": 0}
    for issue in sorted(issues, key=lambda i: (i.get("file", ""), i.get("line") or 0)):
        if issue.get("severity") not in VERIFIED_SEVERITIES:
            continue
        if issue.get("verified") is not True:
            skipped["unconfirmed"] += 1
            continue
        if issue.get("line") not in texts.get(issue.get("file"), {}):
            skipped["outside_diff"] += 1
            continue
        eligible.append(issue)
    current_spots = {(i["file"], i["line"]) for i in eligible}
    # A comment still on a current issue's line belongs to that issue, and must
    # not also be claimed by a moved one.
    for spot in current_spots & at_spot.keys():
        key, anchor = at_spot[spot]
        if anchor:
            anchored[key, anchor] -= 1
    legacy_moved = {key: len(spots - current_spots) for key, spots in legacy.items()}
    comments, posted = [], set(at_spot)
    for issue in eligible:
        key, spot = issue_key(issue), (issue["file"], issue["line"])
        anchor = line_anchor(texts[issue["file"]][issue["line"]])
        if spot in posted:
            skipped["already_posted"] += 1
        elif anchored.get((key, anchor), 0) > 0:
            anchored[key, anchor] -= 1
            skipped["already_posted"] += 1
        elif legacy_moved.get(key, 0) > 0:
            legacy_moved[key] -= 1
            skipped["already_posted"] += 1
        else:
            posted.add(spot)
            comments.append({"path": issue["file"], "line": issue["line"],
                             "side": "RIGHT", "body": inline_body(issue, anchor)})
    return comments, skipped


def post_inline_review(owner: str, repo: str, n: int, snapshot: dict,
                       findings: dict, *, gh=run_gh) -> dict:
    """Post this round's inline comments as one COMMENT review.

    One review, not one comment per issue: GitHub sends a single notification
    for a review, and the comments land together or not at all. COMMENT, not
    REQUEST_CHANGES — the tool advises; it does not gate the merge, and GitHub
    forbids anything else on a PR the token's own user opened.
    """
    if not reviewed(findings):
        return {"posted": 0, "skipped": {}}
    existing = gh(["api", f"repos/{owner}/{repo}/pulls/{n}/comments", "--paginate"])
    comments, skipped = plan_inline(findings.get("code", []),
                                    snapshot.get("files", []), existing or [])
    if not comments:
        return {"posted": 0, "skipped": skipped}
    payload = {
        "event": "COMMENT",
        "body": (f"🔍 Harness code review — {code_verdict_label(findings)}. "
                 f"The full list is in the report comment.\n\n{REVIEW_MARKER}"),
        "comments": comments,
    }
    if snapshot.get("head_sha"):
        payload["commit_id"] = snapshot["head_sha"]
    fd, path = tempfile.mkstemp(prefix="hpr-review-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        gh(["api", f"repos/{owner}/{repo}/pulls/{n}/reviews",
            "-X", "POST", "--input", path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"posted": len(comments), "skipped": skipped}
