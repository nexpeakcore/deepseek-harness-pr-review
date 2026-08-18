# Design: Review Process Status + Live Log Viewer

- Date: 2026-08-16
- Status: Approved (3 sections reviewed)

## Goal

When a review is already running (409 "review already running"), show the
process info (PID, started time, elapsed) inline and stream the live review log
in the UI instead of a bare error alert.

## Part 1 — Lock file metadata

`review.lock` now holds JSON instead of being empty:

```json
{"pid": 12345, "started_at": "2026-08-16T16:08:00"}
```

- `src/run.py` `_acquire_review_lock` writes this on acquire, so the lock covers
  every entry point (manual CLI, web trigger, autoreview poller) rather than only
  web-triggered reviews. `trigger_review` no longer writes it; it clears a stale
  lock before dispatch and `run.py` releases its own in a `finally`
- Lock exists but PID not alive → stale (process died); treated as not running
  so a new review can proceed. `_acquire_review_lock` reclaims it itself — the
  CLI and the poller call `run.main` directly and have no dashboard to clean up
  after them, so `O_EXCL` alone would wedge that PR forever after one crash

## Part 2 — Status + log APIs

- `GET /api/repos/{owner}/{repo}/pr/{n}/review/status`
  → `{running: true, pid, started_at, elapsed_seconds}` or
  `{running: false, stale: true|false}`
- `GET /api/repos/{owner}/{repo}/pr/{n}/review/log?lines=200`
  → `{log: "<last N lines of review.log>", running: bool}`
  (no file → `{log: "", running: false}`)

## Part 3 — Per-PR review log

- `trigger_review` redirects stdout/stderr (`contextlib.redirect_stdout/
  redirect_stderr`) into `session_dir/review.log` while `run.main` executes
- Web-triggered reviews write it; poller runs in its own process (autoreview.log),
  UI only shows review.log when present

## Part 4 — UI

- Repo page row with status `reviewing` → `Reviewing… (PID 12345, started
  2026-08-16T16:08:00)` — `metrics.open_prs` reads lock JSON to add
  `pid`/`started_at` (raw timestamp; elapsed-ago formatting not implemented)
- Trigger while running → inline message (no alert): fetch status, render
  "Review already running — PID …, started …" next to the button
- **Review log panel**: when reviewing (or after clicking Review now), show a
  dark `<pre>` panel with the log tail; auto-refresh every 3s while
  `running: true`; stops when done (no user collapsible control)
- Repo page shows a live log panel (3s polling) for reviewing rows; the raw `started_at` timestamp is shown next to the PID (elapsed-ago formatting not implemented)

## Testing

- Lock JSON written/read with pid + started_at; stale lock (dead PID) →
  running false
- Status API returns running/pid/elapsed; log API returns tail
- Repo page renders PID in reviewing row; 409 → inline status message
- trigger_review writes review.log with captured output (fake run_main prints)
