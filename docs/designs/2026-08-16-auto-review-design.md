# Design: Auto Review Poller

- Date: 2026-08-16
- Status: Approved (3 sections reviewed)

## Goal

Automatically run PR reviews when new PRs are opened (and re-review when the PR
head commit changes) on a configurable list of repos, using a local poller. Runs
on the user's machine — no cloud infra. Review runs in batch mode (`--skip-human`)
and posts the report comment to the PR.

## Approach

Local poller daemon (user chose option A over GitHub Actions / webhook):

- Polls GitHub via `gh api` on an interval, compares against `sessions/`
- Reviews PRs that are new (no session snapshot) or whose head SHA changed
- Reuses the existing pipeline via `run.main(...)` — no new review logic

## Config (`autoreview.yml`, at repo root)

```yaml
org: sample-org            # default org for repo discovery
default_mode: manual        # repos not listed → manual
interval_minutes: 2
post_comment: true
skip_human: true
drafts: false
skip_bots: true            # skip bot PRs (Renovate/Dependabot) in auto review
repos:
  sample-app: auto          # dict mode: repo name → mode (URLs and owner/repo accepted via --add-repo)
```

## Architecture

```
src/
├── autoreview.py       # NEW: poller (--once / --daemon)
├── snapshot.py         # MODIFY: add head_sha to snapshot.json
autoreview.yml          # config
autoreview.log          # per-run log (auto-created)
```

### `autoreview.py` — two modes

- `python -m src.autoreview --once` — single pass (for launchd/cron)
- `python -m src.autoreview --daemon` — loop with `interval_minutes`
- `python -m src.autoreview --once --dry-run` — list PRs that would be
  reviewed without dispatching (for testing against real gh)

### Per-pass logic

1. Read config; for each repo: `gh api repos/{o}/{r}/pulls?state=open`
   → `[{number, head.sha, draft}]`
2. Skip drafts if `drafts: false`
3. For each PR:
   - No `sessions/<o>/<r>/pr-<n>/snapshot.json` → NEW → review
   - Snapshot exists but `head_sha` != current `head.sha` → RE-RUN → review
   - Head matches → SKIP
4. Dispatch via `run.main([owner/repo, n, --skip-human])`; add `--no-post`
   when `post_comment: false` in config
5. Log per PR: `NEW|RE-RUN|SKIPPED|FAILED <owner/repo>#<n> — reason`

### Snapshot change

`build_snapshot` stores `head_sha` (from `meta.head.sha`). Old snapshots without
`head_sha` are treated as never-reviewed (re-review once — safe over missing).

Re-review: delete `findings.json`, `answers.json`, `report.md`, `agent-log.txt`
from the session dir, then call `run.main([..., "--force"])` so all 5 phases run
fresh with the new head. `post_comment` is already idempotent (updates the single
marked comment), so re-review never spams.

## Error Handling

- `gh api` failure (auth, rate limit) → log `POLL-ERROR` for that repo and
  continue to the next repo (no hot retry); the next pass retries
- Repeated failure on one repo (deleted, renamed, 404) → per-repo counter in
  `_repo_failures`; from the 3rd consecutive failure the log line carries
  `(skipping: N consecutive failures)` so a dead repo stops looking like a new
  incident every pass. A successful fetch resets the counter to 0
- One PR failing (model/agent error) → log `FAILED`, continue with other PRs
- Lock file `autoreview.lock` — prevents two concurrent pollers (daemon + cron).
  Dead PID → reclaimed. Alive PID but the lock is older than 4h → PID reuse or a
  hung pass, so it is stolen with a warning. Unparseable lock → reclaimed with a
  warning (returning "held" there would wedge the poller silently forever)
- Missing API key → clear error at startup, exit 3 (consistent with run.py)

## Scheduling (macOS)

- **launchd (recommended)**: `com.nexpeak.pr-review.plist` with
  `StartInterval` 600 → runs `--once`; survives logout
- **cron**: `*/10 * * * * cd /Users/gianglh/work/harness && PYTHONPATH=src .venv/bin/python -m src.autoreview --once >> autoreview.log 2>&1`
- **daemon (dev)**: `python -m src.autoreview --daemon` in terminal/tmux

## Testing

- `test_autoreview.py`: fake gh (PR lists + head SHAs) + fixture sessions dir →
  assert correct NEW / RE-RUN / SKIPPED selection, log statuses, lock file
  behavior, stale-lock takeover, skip-bot filtering
- `test_snapshot.py`: extend fixture with `head_sha` assertion
- E2E: `--once --dry-run` against real gh prints PRs that would be reviewed
  without dispatching
