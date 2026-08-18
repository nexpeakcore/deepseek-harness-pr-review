# Design: Per-Repo Auto/Manual Config + Web UI Management

- Date: 2026-08-16
- Status: Approved (3 sections reviewed)

## Goal

Allow per-repo control of auto review: each repo in an org (or any repo the user
adds) can be set to `auto` (poller reviews its PRs) or `manual` (poller skips it;
review via CLI). The config is editable both from the web dashboard (dedicated /config
page) and the CLI — both write the same `autoreview.yml`.

## Config format (`autoreview.yml`)

```yaml
org: nexpeakcore                # default org for discovery
default_mode: manual            # repos not listed → manual (skipped by poller)
interval_minutes: 10
post_comment: true
skip_human: true
drafts: false
max_parallel: 1                 # PRs reviewed at once per pass (cap 8)
max_agents: 4                   # concurrent agents system-wide, not per review (cap 16)
repos:
  sample-app: auto
  sample-api: auto
  admin-web: manual
```

Backward compatible: old format `repos: [owner/repo]` still loads (treated as
all-auto).

## Components

### `src/autoreview_config.py` (extended)

- `load_config(path) -> dict` — new format + backward compat
- `set_repo_mode(path, repo, mode)` — add/change mode, rewrite file
- `remove_repo(path, repo)` — remove from repos dict
- `list_repos(path, gh) -> [{name, mode: auto|manual|unlisted}]` — org repos
  via `gh api orgs/{org}/repos` (skip if no org), merged with configured modes
- File writes are atomic (write temp + rename)

### `src/autoreview.py` (extended)

- Poller only reviews repos whose mode is `auto`
- New subcommands:
  - `--add-repo <URL|owner/repo|name> --mode auto|manual` (URLs like https://github.com/owner/repo accepted)
  - `--rm-repo <owner/repo|name>`
  - `--repos` — print status table (includes unlisted org repos when `org` set)

### `web/server.py` (extended)

A dedicated `/config` page holds the config management block (separate from the repo dashboard at `/`):

- `/` lists reviewed repos (from sessions) plus auto-configured repos
  without review data (AUTO badge cards).
- The dedicated `/config` page holds the repo config UI: org header + global
  badges (interval, drafts, post_comment); table of org repos with Auto/Manual
  toggle, Remove button for listed repos, "Enable auto" for unlisted repos;
  "Add repo" form; "Refresh".
(The repo dashboard at `/` shows reviewed repos from sessions; the config page is standalone.)

New API routes:
- `GET /api/config` — current config + org repos with per-repo mode
- `POST /api/config/repos/{repo}/mode` body `{"mode": "auto|manual"}`
- `POST /api/config/repos` body `{"repo": "owner/name"}`
- `DELETE /api/config/repos/{repo}`
- All writes go through `autoreview_config` (same source as CLI)
- Config path from `AUTOREVIEW_CONFIG` env (default `autoreview.yml`)

## Error Handling

- Corrupt config YAML → the /config page shows "invalid config: ..."; the repo dashboard at / still renders reviewed repos independently
- Org discovery fails (bad org / no auth) → hide discovery, show config + review
  data, "org lookup failed" badge *(not implemented — org lookup fails silently, configured repos still shown)*
- Concurrent UI+CLI writes → atomic write (temp + rename); poller reads config
  once per pass
- Add non-existent repo → HTTP 400 with clear message *(not implemented — repo existence is not validated on add)*
- API errors: `{detail: "..."}` HTTP 400/404, surfaced via `alert()` in the config page JS

## Testing

- `tests/test_autoreview_config.py`: set_repo_mode add/change, remove_repo,
  list_repos with fake gh, backward compat, corrupt YAML error
- `tests/test_server.py`: POST mode writes real temp config, DELETE removes,
  GET /api/config returns org repos + modes (fake gh), toggle/add/remove write the real config file
- `tests/test_autoreview.py`: poller skips non-auto repos, `--repos` output
- Manual E2E: run server, toggle repo on demo org
