# AI Code Review with DeepSeek — Headless PR Review Automation

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)
[![Built on DeepSeek Harness](https://img.shields.io/badge/built%20on-DeepSeek%20Harness-4dabf7.svg)](https://deepseek.com/harness/en/)

**AI code review with DeepSeek: headless PR review automation** that verifies
PR descriptions claim-by-claim against real code, checks docs against reality,
and flags requirement impact — with human-in-the-loop only when it matters.

## Why

PR descriptions lie. Docs go stale. Manual code review is slow and
inconsistent. This tool runs a DeepSeek Harness agent that:

- **Verifies PR descriptions claim-by-claim** — each sentence of the
  description is checked against the actual code, with `file:line` evidence
- **Reviews PRs that have no description at all** — the ones that need it most.
  Intent is reconstructed from commits, branch name, labels, the linked issue
  and the diff, then the code is checked against its own implied intent:
  unexplained scope creep, behaviour changes with no test or doc
- **Detects stale and fabricated docs** — up to 60% of repo docs are wrong;
  the agent compares them against real code (`MATCH / STALE / WRONG /
  FABRICATED`)
- **Flags requirement impact** — which business requirements a change touches,
  and whether it breaks something (`CHANGED / BROKEN / RISK`)
- **Runs headless** — one command, or an auto-review poller that watches every
  new PR

## Demo

[![Dashboard: PR review with claim-by-claim evidence](docs/screenshots/dashboard-demo.png)](docs/screenshots/dashboard-demo.png)

The tool reviewing its own PR #9. Every sentence of the description became a
numbered claim, each checked against the real code with `file:line` evidence —
and the header is honest about what it found: description partial, 2 risks,
2 stale docs. Tabs split claims, docs, requirement impact and review threads.

Repo-level pages (KPIs, verdict distribution, every open PR with review status)
and live demo data are included — see [Web dashboard](#web-dashboard).

## Features

| | |
|---|---|
| ✅ **Claim verification** | PR description split into verifiable claims, each checked against code with evidence |
| ✅ **No-description fallback** | PR with an empty or boilerplate body: claims are reconstructed from commits, branch, labels, linked issue and the diff, then checked for internal consistency — and the reconstruction is posted back as the description the author should have written |
| ✅ **Docs reality-check** | Docs compared to real code: `MATCH / STALE / WRONG / FABRICATED` |
| ✅ **Requirement impact** | `CHANGED / BROKEN / RISK` analysis per business requirement |
| ✅ **Human-in-the-loop** | ≤20-word confirmation questions only when uncertain — no guessing |
| ✅ **Parallel agents** | One agent per review axis (claims / docs / impact), claims sharded past 15 — so a 40-claim PR cannot starve the docs check. Concurrency is capped globally across every review process |
| ✅ **Ranked doc targets** | Docs are scored against the diff (path proximity, changed symbols, file mentions) before any agent runs — the agent verifies a bounded, reproducible list instead of grepping the repo |
| ✅ **Repo config that stays true** | Adding a repo verifies it exists and is visible to your token first, so a typo cannot become a permanent config entry. `autoreview --check-repos` (and the /config page) flags entries GitHub can no longer reach and removes them in one click |
| ✅ **Auto review poller** | Reviews new PRs automatically, re-reviews when the head commit changes |
| ✅ **Web dashboard** | Repo config management, review triggers (Review now), live review logs, metrics: risks found, doc errors, verdicts, review rounds per repo |
| ✅ **Idempotent PR comments** | One English comment per PR, updated in place — never duplicated |
| ✅ **Traceable** | Every phase writes structured JSON to `sessions/` |

## Install

Requirements: Python 3.10+ (recommended 3.11), `gh` CLI already authenticated.

**One-liner (recommended — auto-detects Python, creates a venv, fixes PATH):**

```bash
curl -fsSL https://raw.githubusercontent.com/nexpeakcore/deepseek-harness-pr-review/main/scripts/install.sh | bash
```

The installer finds a Python 3.10+ interpreter (falls back to Homebrew on
macOS), creates an isolated venv at `~/.harness-pr-review/venv`, installs the
package from GitHub, symlinks `harness-pr-review` + `autoreview` into
`~/.local/bin`, and runs `doctor`. Re-running it updates to the latest version.

**Or install manually:**

```bash
pip install git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git
```

**Or clone for development:**

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'   # zsh needs quotes; SDK comes from PyPI (deepseek-harness-sdk)
```

Then authenticate:

```bash
gh auth login          # required
export DEEPSEEK_API_KEY=sk-...   # see .env.example
harness-pr-review doctor         # verify everything is ready
```

Running the review on Claude instead? Then no DeepSeek key is needed — see
[Agent backends](#agent-backends).

Keys can also live in a `.env` file. Two locations are read, in order:
`./.env` (dev checkout) then `~/.harness-pr-review/.env` (one-liner install,
so the CLI works from any directory). The first file to define a key wins, and
a real environment variable always beats both.

## Agent backends

The review logic does not care which agent runtime reads the workspace, so the
backend is one env var. Everything else — phases, prompts, schemas, report,
comments — is identical either way.

| `HARNESS_PROVIDER` | Runtime | Credentials |
|---|---|---|
| `deepseek` (default) | DeepSeek Harness SDK, composed by `cordis/minimal.cordis.yml` | `DEEPSEEK_API_KEY` |
| `claude` | Headless `claude -p` (Claude Code CLI) | whatever the CLI is already logged in with — a Claude subscription is enough, no extra API key |

```bash
export HARNESS_PROVIDER=claude
export HARNESS_CLAUDE_MODEL=sonnet   # or opus / haiku / a full model id
harness-pr-review doctor             # now checks for the claude binary, not the SDK
harness-pr-review owner/repo 123
```

The phase log names the backend that actually ran, so a review that quietly
used the wrong one is visible in the dashboard:

```
[4/5] verify — starting agents
      4 agents on claude/sonnet: claims-1, claims-2, docs, impact (cap 4 concurrent)
```

**The workspace is an untrusted PR**, and the Claude backend is locked down to
match the sandbox policy the SDK backend runs under:

- Tools are limited to `Read`, `Grep`, `Glob`, `Write` — no shell, no editing
  files outside the part it is asked to write, no network.
- `--safe-mode`, `--setting-sources user` and `--strict-mcp-config` mean a
  `CLAUDE.md`, `.claude/settings.json`, hook or `.mcp.json` **committed inside
  the reviewed repo is not loaded** — a PR does not get to configure the agent
  reviewing it.
- Each agent runs under `--max-budget-usd`, so a loop that stops making
  progress stops spending.

Per-agent cost, session id and any permission denial land in
`sessions/<owner>/<repo>/pr-<n>/claude-<axis>.json`, next to the existing
artefacts.

## Updating

**Easiest — built-in self-update:**

```bash
harness-pr-review update     # installs the latest version from GitHub
harness-pr-review --version  # show the installed version
```

**Or manually:**

Installed via pip (no clone):

```bash
pip install -U git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git
```

Cloned for development:

```bash
git pull origin main   # pull the latest code
pip install -e .       # refresh entry points if pyproject.toml changed
```

**After updating:**

- The auto-review poller (launchd/cron) picks up the new code on its next
  pass — no restart needed.
- A running web dashboard keeps the old code until restarted: stop the
  process, then start it again (`harness-pr-review web`).
- Your existing `sessions/` data and `autoreview.yml` are preserved — updates
  never touch them.

## Usage

After `pip install -e '.[dev]'` you get two commands:

```bash
harness-pr-review doctor                # check readiness: Python, gh, API key, SDK
harness-pr-review owner/repo 123        # review one PR (interactive)
harness-pr-review owner/repo 123 --skip-human   # batch, no questions
harness-pr-review owner/repo 123 --no-post      # don't post a comment
harness-pr-review https://github.com/owner/repo/pull/123  # paste a GitHub PR link
autoreview --once                       # auto review: single pass
autoreview --daemon                     # auto review: every interval_minutes
autoreview --add-repo https://github.com/owner/repo --mode auto  # add by link
```

(Or run from source: `PYTHONPATH=src python -m src.run owner/repo 123`)

Results land in `sessions/<owner>/<repo>/pr-<n>/report.md` (change the directory with `DSH_SESSION_ROOT`).

## Pipeline

1. **Snapshot** — fetch PR metadata, diff files, commits, review threads (GitHub REST + GraphQL)
2. **Claims** — LLM splits the description into verifiable claims
3. **Verify** — the agent backend deep-dives in a disposable worktree:
   verifies each claim, docs reality-check (MATCH/STALE/WRONG/FABRICATED),
   requirement impact, review thread status. DeepSeek Harness by default,
   Claude Code with `HARNESS_PROVIDER=claude` — see [Agent backends](#agent-backends)
4. **Human gate** — asks for confirmation (≤20 words/question) when docs are wrong or claims are uncertain
5. **Synthesize** — English report.md + two comments on the PR:
   - **The report** — one comment, edited in place on every re-review so the PR
     never fills up with stale reports. It opens with a `Review complete` line
     carrying the timestamp, round number and reviewed commit.
   - **A round ping** — a short new comment per round with the headline numbers
     (verdict, risks, doc errors, claim breakdown) and a link up to the report.
     GitHub raises no notification for an edit, so this is the only part that
     actually reaches subscribers. Disable with `--no-ping`, or
     `ping_comment: false` in `autoreview.yml`.

## Running tests

```bash
python -m pytest -v
```

## Web dashboard

Web dashboard for review metrics (PRs reviewed, risks found, doc errors, verdicts
per repo). Reads `sessions/` directly — no database.

```bash
pip install -e '.[web]'
DSH_SESSION_ROOT=sessions harness-pr-review web
harness-pr-review web   # open http://127.0.0.1:6789
```

Pages: repo list → repo detail (KPIs + verdict donut + PR table) → PR detail
(tabs: Claims / Docs / Impact / Threads / Confirm). The PR table lists ALL open
PRs from GitHub with review status (Not reviewed / Reviewing / Reviewed N
rounds / Failed · interrupted — a session that never produced findings and has
no live lock, i.e. the review crashed). Risks counts FAIL + PARTIAL claims and
BROKEN + RISK impacts; Doc errors counts WRONG + FABRICATED + STALE docs. Each open PR row has a
**Review now** / **Re-review** button that runs the review synchronously using
the repo's auto-review config (skip-human + post-comment flags from
`autoreview.yml`).

**Demo data** is checked into `sessions/demo/app/` — start the server and open
http://127.0.0.1:6789/repos/demo/app/pr/7 for a sample review (PR #8 shows a
a CONTRADICTED verdict + FABRICATED doc), useful for screenshots and documentation.

## Auto review

Poll GitHub for new PRs (and head-SHA changes) and review them automatically in
batch mode. Each repo is configured `auto` (poller reviews its PRs) or `manual`
(poller skips it; review via CLI). Edit `autoreview.yml` directly, via CLI, or
from the web dashboard (Config page → toggle Auto/Manual).

`autoreview.yml` is gitignored — copy `autoreview.yml.example` and fill in your
repos. Repo names stay private.

```yaml
# autoreview.yml (copy from autoreview.yml.example)
org: your-org            # default org for repo discovery
default_mode: manual     # repos not listed → manual
interval_minutes: 2
post_comment: true
skip_human: true
drafts: false
skip_bots: true          # skip bot PRs (Renovate/Dependabot)
repos:
  your-repo: auto
  another-repo: manual
```

```bash
autoreview --add-repo sample-app --mode auto   # enable auto
autoreview --rm-repo sample-app                # remove
autoreview --repos                             # list status
autoreview --once          # single pass (cron/launchd)
autoreview --daemon        # loop every interval_minutes
```

launchd example (auto-start on login, every 2 minutes):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nexpeak.pr-review</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/gianglh/work/harness/scripts/autoreview-once.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key>
  <string>/Users/gianglh/work/harness/autoreview.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/gianglh/work/harness/autoreview.log</string>
</dict>
</plist>
```

`scripts/autoreview-once.sh` sources `.env` (API key stays out of the plist).

**Parallel reviews.** `max_parallel` in `autoreview.yml` (default `1`, cap `8`)
sets how many PRs one pass reviews at a time; `1` is the old sequential
behaviour. Each review runs in its own process, and `review.lock` is per-PR, so
two different PRs never share a workspace or a comment. The useful ceiling is
your model API concurrency rather than CPU — roughly 80% of a review's wall
time is spent waiting on the model. `review_timeout_minutes` (default `30`)
kills a hung review so it cannot hold a slot forever.
Install:

```bash
cp com.nexpeak.pr-review.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nexpeak.pr-review.plist
```

Re-review rules: head SHA in the PR changed vs the last snapshot → all phases
re-run with `--force`; the PR comment is updated in place (never duplicated).

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `HARNESS_PROVIDER` | `deepseek` | Agent backend: `deepseek` or `claude` (see [Agent backends](#agent-backends)) |
| `HARNESS_CLAUDE_MODEL` | `sonnet` | Model for the `claude` backend |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key (only required by the `deepseek` backend) |
| `DSH_MODEL` | `deepseek-v4-flash` | Model for the `deepseek` backend (agent + claim extraction) |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible endpoint |
| `DSH_SESSION_ROOT` | `sessions` | Directory storing per-phase results |

## License

[MIT](LICENSE) © 2026 Nexpeak
