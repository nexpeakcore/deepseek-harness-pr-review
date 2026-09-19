# Repository Guidelines

## Project Structure & Module Organization

- `src/` contains the review pipeline and command-line entry points. Keep each
  concern in its focused module (for example, GitHub access in `src/gh.py` and
  agent backends in `src/llm.py` or `src/claude_cli.py`).
- `web/` contains the FastAPI app, metrics helpers, Jinja templates in
  `web/templates/`, and dashboard CSS in `web/static/`.
- `tests/` mirrors production behavior with pytest modules named
  `test_<area>.py`.
- `docs/` holds the static documentation site and design notes; `scripts/`
  contains installation and one-off operational scripts. Runtime review output
  is written to `sessions/` and should not be treated as source code.

## Build, Test, and Development Commands

Create a Python 3.10+ virtual environment and install development dependencies:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -v
```

Use `harness-pr-review doctor` to validate the local CLI, GitHub authentication,
and selected agent backend. For dashboard work, install `.[web]` and run
`DSH_SESSION_ROOT=sessions harness-pr-review web`; it serves locally on port
6789.

## Coding Style & Naming Conventions

Use standard Python conventions: four-space indentation, `snake_case` for
functions and variables, `PascalCase` for classes, and clear module names such
as `repo_check.py`. Preserve type hints and explicit error messages where the
surrounding code uses them. Keep templates semantic and place shared visual
rules in `web/static/style.css`.

When adding or modifying charts, use Apache ECharts exclusively; do not add a
different charting library unless explicitly requested.

## Testing Guidelines

Add or update a focused pytest test for every behavior change. Name tests
`test_<expected_behavior>` and use fixtures or `monkeypatch` to isolate GitHub,
LLM, filesystem, and web calls. Run the full suite with `python -m pytest -v`
before opening a pull request; no coverage threshold is configured, so preserve
or improve coverage for changed paths.

## Commit & Pull Request Guidelines

Follow the established conventional style: `feat: add review axis`,
`fix: handle invalid response`, or `chore: bump version`. Keep commits scoped.
Pull requests should explain the user-visible change, link the relevant issue
or PR when applicable, include tests run, and attach screenshots for dashboard
or documentation UI changes. Do not commit API keys, `.env` files, or generated
session artifacts.
