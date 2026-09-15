# Code review axis

## Problem

Every review axis measured the PR against something outside the code: the
claims agent against the description, the docs agent against the docs, the
impact agent against requirements. None read the diff asking whether it is
*correct*. A PR whose description said "adds retry" and whose code retried on
every 4xx came back ACCURATE — green — because the claim was true.

The headline number did not help. "Risks found" summed FAIL + PARTIAL claims and
BROKEN + RISK impacts. Across 34 real sessions that was ~91 items, of which only
9 (4 FAIL, 5 BROKEN) were the code being wrong; the rest were imprecise
descriptions and statements the agent could not check. PR #26 reported
"Risks found: 4" with no code defect among them.

## Design

### A fourth axis, in two steps

1. **Code agents** — one per shard of changed files (20 files or 120k chars of
   patch, whichever first), running alongside claims/docs/impact. The shard's
   diff is written into the workspace as `review-diff-<name>.patch`, each line
   prefixed with its new-file line number, so the agent reports lines that
   land where the inline comment must go. The agent reads callers, callees and
   tests from the checkout with the same Read/Grep/Glob tools as every other
   axis — still no shell.

   Every issue must carry a concrete failure scenario (input or state → wrong
   result). No scenario, no issue: that one rule separates a defect from a
   style opinion. Severity: BLOCKER (breaks in normal use / data loss /
   security hole), MAJOR (breaks on a realistic edge case or failure path),
   MINOR (real, low impact). Categories: correctness, error-handling,
   security, concurrency, resource, performance, test-gap.

2. **Verify agents** — after the fan-out, every BLOCKER and MAJOR is re-read
   adversarially and answered CONFIRMED or REJECTED per id, rejecting anything
   whose scenario depends on code it cannot find. Issues go in batches of 10,
   one agent per batch, in parallel: a single verifier for every shard's
   issues has no ceiling and runs out of budget partway down a long list. A
   batch that dies leaves only its own issues unconfirmed (`code_meta.verify`
   = `partial`). Verification runs only when there is something to check, so
   a clean PR pays nothing extra.

   Rejected issues move to `findings.json:code_rejected` with the reason —
   kept, not deleted, so the tool's precision can be measured later. An id the
   verifier skips stays, unconfirmed. If the verifier dies, every issue stays
   unconfirmed and a "Review gap" question says so.

### findings.json

```json
"code": [{"id": "K1", "file": "a.py", "line": 42, "severity": "MAJOR",
          "category": "correctness", "title": "...", "scenario": "...",
          "evidence": ["a.py:42"], "verified": true}],
"code_rejected": [{"...": "...", "reason": "guarded by the caller"}],
"code_meta": {"shards": 1, "failed_shards": 0, "verify": "ok|failed|skipped"}
```

`code` is optional. A session from before this axis, or run with
`HARNESS_CODE_REVIEW=0`, has none and reads **Code: not reviewed** — never
"no issues". Every code shard failing reads the same way.

### Two verdicts, not one

The description verdict (ACCURATE / PARTIAL / CONTRADICTED / …) is unchanged
and still measures only the description. The code verdict sits beside it:

| Code verdict | When | Label |
|---|---|---|
| BLOCKER | any blocker survived verify | `Code: 1 blocker · 2 major` |
| MAJOR | any major, no blocker | `Code: 2 major · 1 minor` |
| CLEAN | minor or nothing | `Code: no issues found` / `Code: no blocking issues (3 minor)` |
| NOT_RUN | no code axis in the session | `Code: not reviewed` |

Merging them would make one word carry two unrelated measurements — the same
mistake "Risks found" made with numbers.

### Metrics

| Metric | Counts |
|---|---|
| **Bugs** | code BLOCKER + MAJOR, claim FAIL, impact BROKEN |
| **Needs a look** | claim PARTIAL, impact RISK |
| **Doc errors** | WRONG + FABRICATED + STALE (unchanged) |

All three come from `synthesize.summary_counts()`, which the PR comment, the
round ping and the dashboard now share — the dashboard used to recount on its
own. The impact prompt now says an unverifiable statement is not a RISK and
belongs in `unresolved_questions`.

Historical sessions re-count on read: their PARTIAL claims and RISK impacts
move from the bug count to "Needs a look", so a repo's BUGS FOUND drops.

### Inline comments

After the report comment, confirmed BLOCKER/MAJOR issues are posted as one
review (`POST /pulls/{n}/reviews`, `event: COMMENT`, `commit_id` = the
snapshot's head). One review, so one notification and all-or-nothing
delivery; COMMENT, because the tool advises and GitHub forbids
REQUEST_CHANGES on a PR the token's own user opened.

Not posted inline, but kept in the report:

- MINOR issues and unconfirmed ones — a wrong comment on a line costs trust
  in every later one.
- Lines outside the diff hunks — GitHub rejects the whole review over one.
  `commentable_lines()` computes the accepted set from the patch.
- Issues already posted. Each comment carries
  `<!-- harness-code:<key> -->`, key = hash of file + category + normalized
  title — not the line, which moves when anything above it changes. A new
  round skips an issue on a line this tool already commented on; each earlier
  comment of the same key that no current issue sits on stands for one issue
  whose line moved, and absorbs exactly one. So the same pattern at two lines
  posts twice, a moved issue is not posted again, and a new issue above an old
  one with the same title is still posted.

The diff files the code agents read are written into the PR's own checkout,
where the PR could have committed a symlink at the same name. They are created
with `O_EXCL | O_NOFOLLOW` after removing whatever is there, so the write can
never leave the workspace. `normalize_issues()` drops an issue with no
scenario, so the rule holds even when a model ignores the prompt.

GitHub sends no patch for a text file whose diff is too large. The head
version alone does not say what changed, so such a file cannot be reviewed:
it is listed in `code_meta.patchless_files`, named in the report as not
reviewed, and makes the code verdict `(partial)` instead of clean.

Posting never fails the review: an error is a warning in the log, like the
round ping.

## Cost

One code agent per ~20 changed files plus, when needed, one verify agent — on a
typical PR 1–2 agents on top of the existing 3, all under the global
`HARNESS_MAX_AGENTS` cap. `HARNESS_CODE_REVIEW=0` switches the axis off.

## Not done

- Tracking whether a posted issue disappears in the next round (the natural
  precision signal) — `code_rejected` and the stable key make it possible.
- Resolving the inline thread when its issue is gone.
