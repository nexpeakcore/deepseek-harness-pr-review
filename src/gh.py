"""Thin wrapper around the gh CLI. gh must already be authenticated (gh auth login)."""
import json as _json
import subprocess


def _run_gh_impl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def parse_json_stream(text: str):
    """Parse gh's stdout, which is not always a single JSON document.

    `gh api --paginate` emits ONE document per page, so any response past the
    100-item page size arrives as `[...]\\n[...]` — two valid documents, and not
    valid JSON together. It stayed invisible for a year because every call site
    happened to stay under 100: PR #942 grew from 87 changed files to 120 and
    the review died in phase 1 with "Extra data: line 2 column 1".

    `--slurp` would also fix it, but it returns pages nested (`[[...],[...]]`)
    and gh rejects it alongside `--jq`, so the seam is better handled here:
    one place, no flag juggling, no gh version floor.

    Single-document output (the overwhelmingly common case) decodes on the
    first pass. Multiple list documents are pages of one collection and are
    concatenated; anything else is returned as the list of documents.
    """
    decoder = _json.JSONDecoder()
    docs, idx, end = [], 0, len(text)
    while idx < end:
        while idx < end and text[idx] in " \t\r\n":
            idx += 1
        if idx >= end:
            break
        doc, idx = decoder.raw_decode(text, idx)
        docs.append(doc)
    if not docs:
        raise _json.JSONDecodeError("no JSON document in output", text or "", 0)
    if len(docs) == 1:
        return docs[0]
    if all(isinstance(d, list) for d in docs):
        return [item for page in docs for item in page]
    return docs


def run_gh(args: list[str], *, json: bool = True) -> dict | list:
    proc = _run_gh_impl(args + (["--jq", "."] if json else []))
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed: {proc.stderr.strip()}")
    if not json:
        return proc.stdout
    try:
        return parse_json_stream(proc.stdout)
    except _json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh api returned invalid JSON: {e}. stdout={proc.stdout[:200]!r}"
        ) from e


def gh_available() -> bool:
    try:
        proc = _run_gh_impl(["--version"])
        return proc.returncode == 0
    except FileNotFoundError:
        return False
