import json
import subprocess

import pytest

from src import claude_cli


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _envelope(**over) -> str:
    data = {"type": "result", "subtype": "success", "is_error": False,
            "result": "done", "total_cost_usd": 0.01, "session_id": "abc",
            "permission_denials": []}
    data.update(over)
    return json.dumps(data)


def _spy(stdout="", stderr="", returncode=0):
    """Fake subprocess.run that records the call it was given."""
    calls = []

    def fake(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return _Proc(stdout, stderr, returncode)

    return fake, calls


# --- command line -----------------------------------------------------------

def test_build_argv_hardens_against_the_reviewed_repo():
    """The workspace is an untrusted PR: it must not configure its own reviewer."""
    argv = claude_cli.build_argv(model="sonnet",
                                 allowed_tools=claude_cli.AGENT_TOOLS)
    assert argv[:2] == ["claude", "-p"]
    assert "--safe-mode" in argv          # no CLAUDE.md / hooks / skills from the repo
    assert "--strict-mcp-config" in argv  # no .mcp.json from the repo
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_build_argv_passes_tools_as_one_comma_separated_argument():
    """Variadic space-separated tools would swallow the flag that follows them."""
    argv = claude_cli.build_argv(model="sonnet",
                                 allowed_tools=("Read", "Write"),
                                 system="be brief")
    assert argv[argv.index("--allowedTools") + 1] == "Read,Write"
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"


def test_agent_tools_grant_no_execution():
    """Read the code, write one part file — a PR cannot make the agent run it."""
    assert set(claude_cli.AGENT_TOOLS) == {"Read", "Grep", "Glob", "Write"}


def test_build_argv_omits_optional_flags_when_unset():
    argv = claude_cli.build_argv(model="sonnet", max_budget_usd=None)
    assert "--allowedTools" not in argv
    assert "--append-system-prompt" not in argv
    assert "--max-budget-usd" not in argv


# --- run --------------------------------------------------------------------

def test_run_sends_the_prompt_over_stdin_not_argv(tmp_path):
    """A claims prompt carries the whole diff digest — argv would hit ARG_MAX."""
    fake, calls = _spy(_envelope(result="PONG"))
    prompt = "x" * 5000

    assert claude_cli.run(prompt, model="sonnet", cwd=tmp_path,
                          _run=fake) == "PONG"
    assert calls[0]["input"] == prompt
    assert prompt not in calls[0]["argv"]
    assert calls[0]["cwd"] == str(tmp_path)


def test_run_raises_when_the_cli_reports_an_error():
    fake, _ = _spy(_envelope(is_error=True, subtype="error_during_execution",
                             result="model overloaded"))
    with pytest.raises(RuntimeError, match="model overloaded"):
        claude_cli.run("hi", _run=fake)


def test_run_raises_on_a_non_success_subtype():
    """max_turns / budget stops are is_error=False but produced no answer."""
    fake, _ = _spy(_envelope(subtype="error_max_turns", result=""))
    with pytest.raises(RuntimeError, match="error_max_turns"):
        claude_cli.run("hi", _run=fake)


def test_run_raises_on_empty_result():
    fake, _ = _spy(_envelope(result="   "))
    with pytest.raises(RuntimeError, match="empty result"):
        claude_cli.run("hi", _run=fake)


def test_run_reports_stderr_when_the_cli_prints_nothing():
    fake, _ = _spy("", "not logged in", returncode=1)
    with pytest.raises(RuntimeError, match="not logged in"):
        claude_cli.run("hi", _run=fake)


def test_run_raises_on_non_json_output():
    fake, _ = _spy("Usage: claude [options]")
    with pytest.raises(RuntimeError, match="non-JSON"):
        claude_cli.run("hi", _run=fake)


def test_run_names_the_install_step_when_the_binary_is_missing():
    """The error a user without Claude Code sees has to say what to do next."""
    def fake(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    with pytest.raises(RuntimeError, match="not found on PATH"):
        claude_cli.run("hi", _run=fake)


def test_run_turns_a_timeout_into_a_named_failure():
    def fake(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1800)

    with pytest.raises(RuntimeError, match="timed out after 5s"):
        claude_cli.run("hi", timeout=5, _run=fake)


def test_run_writes_the_meta_sidecar_even_when_the_agent_failed(tmp_path):
    """Cost and session id are the trail a failed agent leaves behind."""
    meta = tmp_path / "sess" / "claude-docs.json"
    fake, _ = _spy(_envelope(is_error=True, subtype="error_during_execution",
                             result="boom", total_cost_usd=0.42))

    with pytest.raises(RuntimeError):
        claude_cli.run("hi", meta_path=meta, _run=fake)

    saved = json.loads(meta.read_text())
    assert saved["total_cost_usd"] == 0.42
    assert saved["session_id"] == "abc"
    # The reply itself already goes to agent-log-<axis>.txt; not stored twice.
    assert "result" not in saved


def test_run_meta_failure_never_sinks_the_run(tmp_path):
    """A sidecar that cannot be written must not cost us the agent's answer."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    fake, _ = _spy(_envelope(result="fine"))

    assert claude_cli.run("hi", meta_path=blocker / "meta.json",
                          _run=fake) == "fine"


# --- salvaging a part file the agent answered inline ------------------------

def test_extract_json_object_reads_a_fenced_reply():
    text = 'Here you go:\n```json\n{"docs": [], "unresolved_questions": []}\n```'
    assert json.loads(claude_cli.extract_json_object(text)) == {
        "docs": [], "unresolved_questions": []}


def test_extract_json_object_stops_at_the_matching_brace():
    text = 'Result: {"a": {"b": 1}} — and some trailing prose {broken'
    assert claude_cli.extract_json_object(text) == '{"a": {"b": 1}}'


def test_extract_json_object_ignores_braces_inside_strings():
    text = '{"note": "a } inside a string", "ok": true}'
    assert json.loads(claude_cli.extract_json_object(text))["ok"] is True


def test_extract_json_object_returns_none_when_there_is_nothing_to_salvage():
    assert claude_cli.extract_json_object("no json here") is None
    assert claude_cli.extract_json_object("") is None
    assert claude_cli.extract_json_object('{"unterminated": ') is None


# --- the chat adapter used by phase 2 ---------------------------------------

def test_chat_matches_the_llm_chat_signature_and_splits_the_roles(monkeypatch):
    """Same signature as src.llm.chat, so extract_claims takes either backend."""
    calls = []
    monkeypatch.setattr(claude_cli, "run",
                        lambda prompt, **kw: calls.append((prompt, kw)) or "[]")

    # api_key / base_url / max_tokens / retries exist only for signature
    # compatibility — the CLI brings its own credentials, cap and retries.
    out = claude_cli.chat(
        [{"role": "system", "content": "you are a claim extractor"},
         {"role": "user", "content": "the PR description"}],
        model="sonnet", api_key="ignored", base_url="http://ignored",
        max_tokens=49_152, retries=3)

    prompt, kwargs = calls[0]
    assert out == "[]"
    assert prompt == "the PR description"
    assert "you are a claim extractor" in kwargs["system"]
    # Phase 2 has no workspace, so a tool call is a wasted turn.
    assert claude_cli.NO_TOOLS_HINT in kwargs["system"]
    assert kwargs["model"] == "sonnet"
