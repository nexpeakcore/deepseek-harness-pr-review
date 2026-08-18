# Design: Repo Page — Open PRs, Review Status, Rounds, Metric Fix

- Date: 2026-08-16
- Status: Approved (3 sections reviewed)

## Goal

Fix `/repos/{owner}/{repo}` so it lists ALL open PRs (not just reviewed ones),
shows each PR's review status (not reviewed / reviewing / failed / reviewed N rounds),
and corrects the Risks / Doc errors numbers so they match what the PR detail page
shows.

## Part 1 — Repo page table (all open PRs)

Table columns: `# | Title | Draft | Review status | Risks | Doc errors`

- Source of open PRs: `gh api repos/{o}/{r}/pulls?state=open`
  (metrics.open_prs duplicates the gh call directly — kept separate to avoid a web→autoreview import)
- Review status per PR (from sessions/):
  - `Not reviewed` — no session dir
  - `Reviewing…` — a live `review.lock` (PID alive), whether or not findings
    exist yet; a live lock with findings means a re-review is running
  - `Failed · interrupted` — session dir exists, findings.json missing, and no
    live lock: the review crashed or was killed. Checked after the live-lock
    branch, so a running review is never mislabelled
  - `Reviewed · N rounds` — findings.json exists, N from rounds.txt (fallback 1)
- Merged/closed PRs with sessions are NOT shown in the table but still counted in KPIs
- KPI cards stay: PRs REVIEWED / RISKS FOUND / DOC ERRORS / OPEN Qs / VERDICTS
- Draft badge shown; gh failure → table shows reviewed PRs only + "open PRs unavailable" badge
- Sort: open PRs by number desc (newest first)

## Part 2 — Round tracking + broader metrics

**Round tracking (pipeline):**
- `run.py`: when `run_verify` actually runs (findings regenerated) → increment
  `session_dir/rounds.txt` (+1)
- Auto re-review (`--force` via autoreview) goes through run.py → counted automatically
- PR with findings but no rounds.txt (legacy data) → display 1
- Manual run without `--force` (cache hit) → no increment

**Broader metrics (`web/metrics.py`):**
- `risks` = claims `FAIL` + `PARTIAL` + impact `BROKEN` + `RISK` (internal key: bugs)
- `doc_errors` = docs `WRONG` + `FABRICATED` + `STALE`
- Demo fixture (tests/test_metrics.py): claims PARTIAL/RISK/STALE combinations assert the counting rules
  → risks = 4, doc_errors = 3 (covered by test_metrics.py::test_pr_record_wider_metrics: FAIL+PARTIAL claims, BROKEN+RISK impacts, WRONG+FABRICATED+STALE docs)

**Data flow:**
- `metrics.pr_record` adds `rounds` (from rounds.txt, fallback 1)
- `metrics.open_prs(session_root, owner, repo, gh)` → merge open PRs (gh) +
  session state → list of rows
- `server.repo_page` calls `open_prs` + renders new table
- `repo.html` adds Draft + Review status columns

## Part 3 — Error handling & edge cases

- gh failure fetching open PRs → show reviewed PRs from sessions + badge
- Draft PRs shown with badge
- Corrupt rounds.txt (not a number) → treated as 1
- Session dir without findings, live lock → "Reviewing…", no crash
- Session dir without findings, no live lock → "Failed · interrupted". The PR
  detail page reports the same state, so the two pages never disagree
- KPI verdict donut counts only reviewed PRs (unchanged)

## Testing

- `test_metrics.py`: rounds from file / fallback 1; new metric definitions
  (PARTIAL/RISK/STALE counted); open_prs merge with fake gh
- `test_run.py`: verify run → rounds.txt incremented; cached run → no increment
- `test_server.py`: repo page shows un-reviewed open PR + "Not reviewed" + rounds
  (fake gh); gh failure badge
- Smoke: `/repos/sample-org/sample-app` shows #77 Reviewed 1 round + #78/#1
  Not reviewed
