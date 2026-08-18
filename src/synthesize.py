"""Phase 5: synthesize the report + post an English comment on the PR."""
import time
from pathlib import Path

from src.gh import run_gh

MARKER = "<!-- harness-pr-review -->"
# Deliberately not a superstring of MARKER: post_comment PATCHes the first
# comment containing MARKER, so a ping carrying it would be overwritten by the
# next full report. test_ping_marker_is_not_confused_with_main_marker pins this.
PING_MARKER = "<!-- harness-pr-review-ping -->"
STATUS_LABELS = {"PASS": "Matches", "FAIL": "Mismatch", "PARTIAL": "Partial",
                 "UNVERIFIED": "Unverified"}


def _cell(text, max_len=200):
    text = str(text or "")
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text.replace("|", "\\|").replace("\n", "<br>")


def _bullet(text, max_len=100):
    text = str(text or "")
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text.replace("\n", " ")


def summary_counts(findings: dict) -> dict:
    """Headline numbers, shared by the full comment and the round ping."""
    claims = findings.get("claims", [])
    by_status = {k: sum(1 for c in claims if c.get("status") == k)
                 for k in ("PASS", "FAIL", "PARTIAL", "UNVERIFIED")}
    risks = by_status["FAIL"] + by_status["PARTIAL"]
    risks += sum(1 for i in findings.get("impact", [])
                 if i.get("impact") in ("BROKEN", "RISK"))
    return {
        "claims": len(claims),
        "by_status": by_status,
        "risks": risks,
        "doc_errors": sum(1 for d in findings.get("docs", [])
                          if d.get("status") in ("WRONG", "FABRICATED", "STALE")),
    }


def _overall_verdict(findings: dict) -> str:
    statuses = [c["status"] for c in findings.get("claims", [])]
    if not statuses:
        return "NO CLAIMS"
    if any(s == "FAIL" for s in statuses):
        return "MISLEADING"
    if any(s in ("PARTIAL", "UNVERIFIED") for s in statuses):
        return "PARTIAL"
    return "ACCURATE"


def build_report(snapshot: dict, claims: list[dict], findings: dict,
                 answers: list[dict], session_dir: Path) -> str:
    """Write report.md. Returns the report content."""
    verdict = _overall_verdict(findings)
    text_by_id = {cl["id"]: cl.get("text", "") for cl in claims}
    lines = [
        f"# Review PR #{snapshot['pr']} — {snapshot['title']}",
        "",
        f"- Author: {snapshot['author']} | Base: {snapshot['base']} → Head: {snapshot['head']}",
        f"- Files changed: {len(snapshot['files'])} | Commits: {len(snapshot['commits'])}",
        f"## Verdict: {verdict}",
        "",
        "## Claims",
        "",
        "| Claim | Content | Status | Evidence | Notes |",
        "|---|---|---|---|---|",
    ]
    for c in findings.get("claims", []):
        text = text_by_id.get(c["id"], c.get("text", ""))
        lines.append(
            f"| {c['id']} | {_cell(text)} | {STATUS_LABELS.get(c['status'], c['status'])} | "
            f"{_cell(', '.join(c.get('evidence', [])) or '-')} | {_cell(c.get('note', ''))} |")
    lines += [
        "", "## Docs vs reality", "",
        "| Doc | Status | Difference |", "|---|---|---|",
    ]
    for d in findings.get("docs", []):
        lines.append(f"| {_cell(d['path'])} | {d['status']} | {_cell(d.get('what', ''))} |")
    lines += [
        "", "## Requirement impact", "",
        "| Requirement | Impact | Detail |", "|---|---|---|",
    ]
    for i in findings.get("impact", []):
        lines.append(f"| {_cell(i['requirement'])} | {i['impact']} | {_cell(i.get('detail', ''))} |")
    lines += [
        "", "## Review threads", "",
        "| Comment | Status | Notes |", "|---|---|---|",
    ]
    for t in findings.get("threads", []):
        lines.append(f"| {_cell(t['text'], max_len=120)} | {t['status']} | {_cell(t.get('note', ''))} |")
    lines += ["", "## Confirmation log", ""]
    for a in answers:
        lines.append(f"- **{a['question']}** → {a['answer']}")
    if not answers:
        lines.append("- (none)")
    report = "\n".join(lines) + "\n"

    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "report.md").write_text(report)
    return report


VERDICT_BADGE = {
    "ACCURATE": ("#27ae60", "Description accurate"),
    "PARTIAL": ("#b9770e", "Description partial"),
    "MISLEADING": ("#c0392b", "Description misleading"),
    "NO CLAIMS": ("#6b7280", "No claims"),
}
STATUS_COLORS = {
    "MATCHES": "#27ae60", "PASS": "#27ae60", "RESOLVED": "#27ae60",
    "FIXED": "#27ae60", "MATCH": "#27ae60",
    "PARTIAL": "#b9770e", "STALE": "#b9770e", "STILL_VALID": "#b9770e",
    "RISK": "#b9770e", "CHANGED": "#b9770e", "OUTDATED": "#b9770e",
    "MISMATCH": "#c0392b", "FAIL": "#c0392b", "WRONG": "#c0392b",
    "FABRICATED": "#8e1c1c", "BROKEN": "#c0392b",
    "UNVERIFIED": "#6b7280", "NO_CLAIMS": "#6b7280",
    "UNAFFECTED": "#6b7280",
}


def _badge(text: str, color: str) -> str:
    """Badge with a colored dot + tinted background (GitHub allows inline styles)."""
    return (f'<span style="background-color:{color}1A;color:{color};'
            f'padding:2px 10px;border-radius:10px;font-size:12px;'
            f'font-weight:600">● {text}</span>')


def _html_cell(text, color=None):
    if color:
        return (f'<td style="padding:6px 10px;border:1px solid #e3e8ee;'
                f'font-size:13px;vertical-align:top">'
                f'<span style="color:{color};font-weight:600">{_html_escape(text)}</span></td>')
    return (f'<td style="padding:6px 10px;border:1px solid #e3e8ee;'
            f'font-size:13px;vertical-align:top">{_html_escape(text)}</td>')


def _html_escape(text) -> str:
    """Escape and sanitize for HTML table cells."""
    import html as _html

    return _html.escape(str(text or ""))


def _summary_table(headers: list[str], rows: list[list[str]],
                   color_cols: set[int]) -> str:
    """HTML table with colored status cells for the comment."""
    head = "".join(
        f'<th style="padding:6px 10px;border:1px solid #e3e8ee;'
        f'background:#f6f8fa;text-align:left;font-size:12px">{_html_escape(h)}</th>'
        for h in headers)
    body = ""
    for row in rows:
        cells = []
        for idx, val in enumerate(row):
            color = STATUS_COLORS.get(str(val).upper()) if idx in color_cols else None
            cells.append(_html_cell(val, color))
        body += f"<tr>{''.join(cells)}</tr>"
    return (f'<table style="border-collapse:collapse;width:100%">'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')


def _comment_section(title: str, icon: str, table: str, open: bool = False,
                     count: int = 0) -> str:
    suffix = f" ({count})" if count else ""
    state = " open" if open else ""
    return (f'<details{state}><summary style="cursor:pointer;'
            f'font-weight:600;font-size:14px;margin:8px 0">{icon} '
            f'{_html_escape(title)}{suffix}</summary>\n\n{table}\n\n</details>')


def _completion_line(snapshot: dict, rounds: int | None,
                     completed_at: str | None) -> str:
    """One line telling the reader this round finished, when, and on what commit.

    The comment is edited in place on every re-review so a PR only ever carries
    one of them. GitHub sends no notification for an edit, so without this line
    a reader has no way to tell a finished re-review from the previous round's
    text — and no way to tell whether it covered their latest push.
    """
    when = completed_at or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    parts = [f"**Review complete** · {when}"]
    if rounds:
        parts.append(f"round {rounds}")
    sha = (snapshot.get("head_sha") or "")[:7]
    if sha:
        parts.append(f"commit `{sha}`")
    return (f"✅ {' · '.join(parts)} — this comment is updated in place on each "
            f"re-review, so check the timestamp instead of waiting for a "
            f"notification.")


def build_comment(snapshot: dict, claims: list[dict], findings: dict,
                  answers: list[dict], report_content: str | None = None,
                  rounds: int | None = None,
                  completed_at: str | None = None) -> str:
    """Build the English comment (single one, with marker).

    Colored, collapsible sections (GitHub allows inline styles; scripts are
    stripped so tabs are rendered as <details> blocks).

    completed_at is injectable so tests get a deterministic body.
    """
    verdict = _overall_verdict(findings)
    v_color, v_text = VERDICT_BADGE.get(verdict, ("#6b7280", verdict))

    # Claims table: id, text, category, status, evidence, note
    text_by_id = {cl.get("id"): cl for cl in claims}
    claim_rows = []
    for c in findings.get("claims", []):
        base = text_by_id.get(c.get("id"), {})
        claim_rows.append([
            c.get("id", ""),
            f"{base.get('text', '')}",
            base.get("category", ""),
            c.get("status", ""),
            ", ".join(c.get("evidence", [])) or "—",
            c.get("note", ""),
        ])
    claims_table = _summary_table(
        ["Claim", "Content", "Category", "Status", "Evidence", "Notes"],
        claim_rows, color_cols={3}) or "- none"

    docs_table = _summary_table(
        ["Doc", "Status", "Difference"],
        [[d.get("path", ""), d.get("status", ""), d.get("what", "")]
         for d in findings.get("docs", [])],
        color_cols={1}) or "- none"

    impact_table = _summary_table(
        ["Requirement", "Impact", "Detail"],
        [[i.get("requirement", ""), i.get("impact", ""), i.get("detail", "")]
         for i in findings.get("impact", [])],
        color_cols={1}) or "- none"

    thread_table = _summary_table(
        ["Comment", "Status", "Note"],
        [[t.get("text", ""), t.get("status", ""), t.get("note", "")]
         for t in findings.get("threads", [])],
        color_cols={1}) or "- none"

    confirm_rows = [[a.get("question", ""), a.get("answer", "")]
                    for a in answers]
    confirm_table = _summary_table(
        ["Question", "Answer"], confirm_rows, color_cols=set()) or "- none"

    sections = [
        _comment_section("Claims", "🟢", claims_table, open=True,
                         count=len(findings.get("claims", []))),
        _comment_section("Docs vs reality", "📄", docs_table,
                         count=len(findings.get("docs", []))),
        _comment_section("Requirement impact", "🎯", impact_table,
                         count=len(findings.get("impact", []))),
        _comment_section("Review threads", "💬", thread_table,
                         count=len(findings.get("threads", []))),
        _comment_section("Confirm log", "✅", confirm_table,
                         count=len(answers)),
    ]

    counts = summary_counts(findings)
    bugs, doc_errors = counts["risks"], counts["doc_errors"]
    summary = (
        f"{_badge(v_text, v_color)} "
        f"{_badge(f'Risks found: {bugs}', '#c0392b' if bugs else '#6b7280')} "
        f"{_badge(f'Doc errors: {doc_errors}', '#b9770e' if doc_errors else '#6b7280')}"
    )
    return (
        f"## Harness PR Review — Verdict: {summary}\n\n"
        f"{_completion_line(snapshot, rounds, completed_at)}\n\n"
        f"{chr(10).join(sections)}\n\n{MARKER}"
    )


def build_ping(snapshot: dict, findings: dict, rounds: int | None = None,
               report_url: str | None = None,
               completed_at: str | None = None) -> str:
    """Short per-round comment carrying the headline numbers.

    Exists purely to raise a notification. The full report lives in one comment
    that is edited in place, and GitHub notifies nobody about an edit, so
    without a fresh comment a finished re-review is invisible until someone
    happens to open the PR. Kept to two lines so a PR with many pushes reads as
    a round log rather than as spam.
    """
    c = summary_counts(findings)
    when = completed_at or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    verdict = VERDICT_BADGE.get(_overall_verdict(findings),
                                ("", _overall_verdict(findings)))[1]
    st = c["by_status"]
    breakdown = ", ".join(
        f"{n} {STATUS_LABELS[k].lower()}"
        for k, n in st.items() if n)

    head = f"round {rounds}" if rounds else "review"
    sha = (snapshot.get("head_sha") or "")[:7]
    where = f" · commit `{sha}`" if sha else ""
    line1 = f"🔍 **Harness review — {head} done**{where} · {when}"
    line2 = (f"{verdict} · **{c['risks']}** risk"
             f"{'' if c['risks'] == 1 else 's'} · "
             f"**{c['doc_errors']}** doc error"
             f"{'' if c['doc_errors'] == 1 else 's'} · "
             f"{c['claims']} claim{'' if c['claims'] == 1 else 's'}"
             f"{f' ({breakdown})' if breakdown else ''}")
    link = (f"\n\n[Full report ↑]({report_url})" if report_url else "")
    return f"{line1}\n\n{line2}{link}\n\n{PING_MARKER}"


def find_report_comment(owner: str, repo: str, n: int, *, gh=run_gh,
                        list_comments=None) -> dict | None:
    """The single marked report comment, or None. Never matches a ping."""
    if list_comments is None:
        list_comments = lambda: gh(
            ["api", f"repos/{owner}/{repo}/issues/{n}/comments", "--paginate"])
    for c in list_comments():
        if MARKER in c.get("body", ""):
            return c
    return None


def post_ping(owner: str, repo: str, n: int, body: str, *, gh=run_gh) -> None:
    """Post the round ping as a NEW comment — that is what notifies people."""
    _post_body(gh, ["api", f"repos/{owner}/{repo}/issues/{n}/comments"], body)


def _post_body(gh, args_prefix: list[str], body: str) -> None:
    """POST/PATCH with the body read from a temp file.

    `-F body=@file` keeps the payload out of the argv, so a very large HTML
    comment (many claims/docs) cannot hit ARG_MAX or shell-quoting limits.
    """
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="hpr-comment-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        gh([*args_prefix, "-F", f"body=@{path}"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def post_comment(owner: str, repo: str, n: int, body: str, *,
                 gh=run_gh, list_comments=None) -> bool:
    """Post a comment if there is no marker; otherwise UPDATE the existing comment (keep a single comment).

    Returns True if a new one was created, False if an existing comment was updated.
    """
    if list_comments is None:
        list_comments = lambda: gh(["api", f"repos/{owner}/{repo}/issues/{n}/comments", "--paginate"])
    for c in list_comments():
        if MARKER in c.get("body", ""):
            _post_body(gh, ["api", f"repos/{owner}/{repo}/issues/comments/{c['id']}",
                            "-X", "PATCH"], body)
            return False
    _post_body(gh, ["api", f"repos/{owner}/{repo}/issues/{n}/comments"], body)
    return True
