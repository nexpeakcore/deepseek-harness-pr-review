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

Re-review: delete `findings.json`, `answers.json`, `report.md` and every
`agent-log*.txt` (verify fans out one agent per axis, so there is one log per
agent, not one per review) from the session dir, then call `run.main([..., "--force"])` so all 5 phases run
fresh with the new head. `post_comment` is already idempotent (updates the single
marked comment), so re-review never spams.

The flip side of editing in place is that GitHub raises no notification for it,
so a reader cannot tell a finished re-review from the round before it. Two
things address that:

1. The report comment opens with a `Review complete` line: timestamp, round
   number and the short head SHA reviewed. The SHA answers the question people
   actually have — did this cover my latest push? This helps only once someone
   opens the PR.
2. A **round ping** — a short NEW comment per round (`post_ping`, marker
   `<!-- harness-pr-review-ping -->`). Being new is the whole point: that is
   what GitHub notifies on. It carries verdict, risk count, doc-error count and
   the claim breakdown, plus a link up to the report comment.

`PING_MARKER` is deliberately not a superstring of `MARKER`. `post_comment`
PATCHes the first comment containing `MARKER`, so a ping carrying it would be
overwritten by the next full report and the round log would silently vanish.

Both counts come from `summary_counts()` so the ping and the report cannot
drift apart. A failing ping is logged and swallowed: losing a notification is
annoying, losing the review because the notification failed is worse.

## Error Handling

- `gh api` failure (auth, rate limit) → log `POLL-ERROR` for that repo and
  continue to the next repo (no hot retry); the next pass retries
- Repeated failure on one repo (deleted, renamed, 404) → per-repo counter in
  `_repo_failures`; from the 3rd consecutive failure the log line carries
  `(skipping: N consecutive failures)` so a dead repo stops looking like a new
  incident every pass. A successful fetch resets the counter to 0
- One PR failing (model/agent error) → log `FAILED`, continue with other PRs
- A review that hangs → killed after `review_timeout_minutes` (default 30) and
  logged as a timeout, so it cannot hold a parallel slot forever. Its
  `review.lock` holds a dead PID and is reclaimed on the next attempt
- Lock file `autoreview.lock` — prevents two concurrent pollers (daemon + cron).
  Dead PID → reclaimed. Alive PID but the lock is older than 4h → PID reuse or a
  hung pass, so it is stolen with a warning. Unparseable lock → reclaimed with a
  warning (returning "held" there would wedge the poller silently forever)
- Missing API key → clear error at startup, exit 3 (consistent with run.py)

## Parallelism

Each review runs as its own process (`python -m src.run`, see
`src/review_proc.py`), never in the poller's or the web server's interpreter.
That is what makes concurrency possible at all: the old in-process path used
`contextlib.redirect_stdout`, which mutates `sys.stdout` for the whole
interpreter, so two reviews would interleave their logs and the second to
finish would restore a stream the first had already closed. A subprocess also
makes `review.lock` record the review's own PID instead of the long-lived
server's, so a review that dies is correctly seen as dead.

- `max_parallel` (default `1`, cap `8`) — reviews running at once in one pass.
  `1` keeps the old strictly-sequential behaviour.
- `max_agents` (default `4`, cap `16`) — concurrent model agents across the
  whole system, not per review. Each review fans out one agent per axis and
  shards claims past 15, so in-flight calls are `max_parallel × agents per
  review` — a product neither side can see alone. Enforced by lock files in
  `.agent-slots/` (`src/agent_pool.py`) and passed to review subprocesses as
  `HARNESS_MAX_AGENTS`, so the poller and a manual CLI run share one budget.
- A pass plans every repo first (sequential `gh` calls, cheap), then fans the
  queued PRs out through a `ThreadPoolExecutor`. Threads are fine because the
  work is a blocked `subprocess.run`, not Python.
- Safe because `review.lock` is per-PR and each PR has its own workspace: two
  different PRs share nothing. The same PR is still limited to one review.
- The real ceiling is model API concurrency, not CPU — measured on this repo,
  a review spends ~80% of its wall time waiting on the model (verify phase
  7m22s of a 9m12s run). Measured: PRs #4 and #5 reviewed concurrently
  finished in 299s wall versus 524s summed, with separate logs and both locks
  released.

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
