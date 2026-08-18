"""Rank the docs a change could have invalidated, before any agent runs.

The verify prompt used to say "read every docs file related to the changed
code" and left "related" to the agent, which meant an unbounded grep over the
repo whose cost and coverage varied run to run. Ranking here instead turns that
into a bounded, reproducible list the agent verifies — cheap string work, no
model call.

The list is a *starting set*, not a closed one: the prompt still allows the
agent to open a doc that is not on it, so a bad score costs coverage only when
the agent also fails to notice.
"""
import os
import re
from pathlib import Path

# No .txt: in practice it is requirements.txt and SOURCES.txt, never prose.
# .html is included because published guides live there (docs/guide/*.html);
# generated HTML is excluded by SKIP_DIRS, not by suffix.
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".html")
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "dist", "build",
    "target", "vendor", ".next", ".nuxt", "coverage", "sessions", ".agent-slots",
    "site", "_site", "public", "htmlcov",
}
# Generated trees that carry no authored prose but do carry matching names.
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info")
MAX_DOC_BYTES = 200_000       # a generated changelog is not worth scanning
MAX_DOCS_SCANNED = 800
DEFAULT_TOP_N = 25

# Declarations added or removed by the diff. Language-agnostic on purpose: a
# name that appears in both the diff and a doc is a hit regardless of syntax.
_DECL_PATTERNS = [
    re.compile(r"^[+-]\s*(?:export\s+)?(?:public\s+|private\s+|static\s+)*"
               r"(?:async\s+)?(?:def|class|func|function|interface|type|struct|enum|trait|impl)\s+"
               r"([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^[+-]\s*(?:export\s+)?(?:const|let|var)\s+"
               r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]"),
    re.compile(r"^[+-]\s*([A-Z][A-Z0-9_]{2,})\s*[:=]"),
]
# Names too generic for a doc mention to mean anything.
_STOP_SYMBOLS = {"self", "data", "value", "result", "item", "args", "kwargs",
                 "true", "false", "none", "null", "test", "main", "init"}

# Scoring weights. Tuned by hand, not by data — the ordering matters more than
# the absolute numbers, and every candidate is verified by the agent anyway.
W_CLAIM_NAMED = 20    # a claim explicitly points at this doc
W_PATH_DEPTH = 3      # per shared leading path component
W_BASENAME = 4        # doc mentions a changed file's name
W_SYMBOL = 3          # doc mentions a symbol the diff declared
W_ROOT_DOC = 1        # README/CHANGELOG: always a candidate, never a strong one
CAP_BASENAME = 12
CAP_SYMBOL = 15
CAP_PATH = 9

_ROOT_DOCS = {"readme", "changelog", "contributing", "architecture", "usage"}


def changed_symbols(snapshot: dict) -> set[str]:
    """Identifiers the diff declares or removes — the words a stale doc repeats."""
    symbols = set()
    for f in snapshot.get("files") or []:
        for line in (f.get("patch") or "").splitlines():
            for pattern in _DECL_PATTERNS:
                m = pattern.match(line)
                if m and len(m.group(1)) >= 3 and m.group(1).lower() not in _STOP_SYMBOLS:
                    symbols.add(m.group(1))
    return symbols


def _iter_docs(workspace: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")
                       and not d.endswith(SKIP_DIR_SUFFIXES)]
        for name in sorted(filenames):
            if not name.lower().endswith(DOC_SUFFIXES):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > MAX_DOC_BYTES:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= MAX_DOCS_SCANNED:
                return


def _shared_depth(a: str, b: str) -> int:
    pa, pb = a.split("/")[:-1], b.split("/")[:-1]
    shared = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        shared += 1
    return shared


def rank_docs(workspace: Path, snapshot: dict, claims: list[dict] | None = None,
              top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """Docs most likely invalidated by this diff, best first.

    Returns [{"path", "score", "why"}] — `why` is carried into the prompt so the
    agent knows what to look for in each file instead of re-deriving it.
    """
    changed = [f.get("filename", "") for f in snapshot.get("files") or []]
    basenames = {Path(c).name for c in changed if c}
    symbols = changed_symbols(snapshot)
    named = {d for c in (claims or []) for d in (c.get("docs") or [])}

    ranked = []
    for path in _iter_docs(workspace):
        rel = path.relative_to(workspace).as_posix()
        # A doc changed by this very PR is reviewed as code, not as a stale doc.
        if rel in changed:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue

        score, why = 0, []
        if rel in named:
            score += W_CLAIM_NAMED
            why.append("named by a claim")

        depth = max((_shared_depth(rel, c) for c in changed), default=0)
        if depth:
            score += min(depth * W_PATH_DEPTH, CAP_PATH)
            why.append(f"sits {depth} level(s) deep with changed files")

        hit_names = sorted(n for n in basenames if n and n in text)
        if hit_names:
            score += min(len(hit_names) * W_BASENAME, CAP_BASENAME)
            why.append(f"mentions {', '.join(hit_names[:3])}")

        hit_symbols = sorted(s for s in symbols if re.search(rf"\b{re.escape(s)}\b", text))
        if hit_symbols:
            score += min(len(hit_symbols) * W_SYMBOL, CAP_SYMBOL)
            why.append(f"mentions changed symbol {', '.join(hit_symbols[:3])}")

        if path.stem.lower() in _ROOT_DOCS:
            score += W_ROOT_DOC
            why.append("top-level project doc")

        if score:
            ranked.append({"path": rel, "score": score, "why": "; ".join(why)})

    ranked.sort(key=lambda d: (-d["score"], d["path"]))
    return ranked[:top_n]
