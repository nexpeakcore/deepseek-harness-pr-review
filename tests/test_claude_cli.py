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
    argv = claude_cli.build_argv(model="sonnet", tools=claude_cli.AGENT_TOOLS)
    assert argv[:2] == ["claude", "-p"]
    assert "--safe-mode" in argv          # no CLAUDE.md / hooks / skills from the repo
    assert "--strict-mcp-config" in argv  # no .mcp.json from the repo
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_build_argv_passes_tools_as_one_comma_separated_argument():
    """Variadic space-separated tools would swallow the flag that follows them."""
    argv = claude_cli.build_argv(model="sonnet", tools=("Read", "Write"),
                                 system="be brief")
    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Write"
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"


def test_tools_is_the_boundary_not_allowedtools():
    """--allowedTools only pre-approves; a tool left out of it still exists.

    Restricting reach with --allowedTools alone leaves Bash present and merely
    unapproved, and a user settings rule can approve it — so the tool set has
    to be cut with --tools, which decides what exists at all.
    """
    argv = claude_cli.build_argv(model="sonnet", tools=claude_cli.AGENT_TOOLS)
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Write"
    assert "Bash" not in argv[argv.index("--tools") + 1]


def test_no_tools_is_the_default_and_removes_every_tool():
    """A new call site has to ask for reach rather than inherit it.

    "" is the CLI's own spelling for "no tools at all" — verified against the
    binary: with it, an instruction to run Bash produces no tool call and no
    file on disk.
    """
    argv = claude_cli.build_argv(model="sonnet")
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv


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


def test_run_names_the_install_step_when_the_binary_is_missing(monkeypatch):
    """The error has to say what to do next — and where it already looked.

    The usual cause is not a missing install but a launcher (launchd, cron)
    handing the process a PATH without the per-user bin dir: the same review
    then works by hand and fails on a schedule, which is unreadable unless the
    PATH is in the message.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    def fake(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    with pytest.raises(RuntimeError, match="not found on PATH") as e:
        claude_cli.run("hi", _run=fake)
    assert "PATH was: /usr/bin:/bin" in str(e.value)


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


# --- retry on a failure that happened inside the API ------------------------

def _sequence(*stdouts):
    """Fake subprocess.run returning a different stdout per call."""
    calls = []

    def fake(argv, **kwargs):
        calls.append(kwargs)
        return _Proc(stdouts[min(len(calls) - 1, len(stdouts) - 1)])

    return fake, calls


API_ERROR = _envelope(is_error=True, terminal_reason="api_error",
                      result="Not logged in · Please run /login")


def test_run_retries_a_failure_inside_the_api():
    """Four agents once died together on a blip while the CLI was logged in.

    terminal_reason api_error, one turn, zero cost — and because nothing tried
    twice, one transient moment cost the whole review.
    """
    fake, calls = _sequence(API_ERROR, _envelope(result="recovered"))
    slept = []

    assert claude_cli.run("hi", _run=fake, _sleep=slept.append) == "recovered"
    assert len(calls) == 2
    assert slept == [2]


def test_run_gives_up_after_the_last_attempt():
    fake, calls = _sequence(API_ERROR)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        claude_cli.run("hi", _run=fake, _sleep=lambda _: None)
    assert len(calls) == 3


def test_run_does_not_retry_what_will_fail_again():
    """A bad model id costs the review time and reaches the same answer."""
    fake, calls = _sequence(_envelope(is_error=True,
                                      subtype="error_during_execution",
                                      result="unknown model"))

    with pytest.raises(RuntimeError, match="unknown model"):
        claude_cli.run("hi", _run=fake, _sleep=lambda _: None)
    assert len(calls) == 1


def test_run_clears_the_previous_attempt_output_before_retrying(tmp_path):
    """An attempt can write its file and then die in the API.

    If the retry answers inline instead of writing, the caller's salvage sees
    a file already sitting there, skips, and read_part() consumes the dead
    attempt's half-written JSON as this agent's findings.
    """
    part = tmp_path / "findings-docs.json"
    part.write_text('{"docs": [{"path": "stale"}]}')
    seen = []
    fake, _ = _sequence(API_ERROR, _envelope(result="answered inline"))

    def watch(argv, **kwargs):
        seen.append(part.exists())
        return fake(argv, **kwargs)

    claude_cli.run("hi", reset_paths=(part,), _run=watch,
                   _sleep=lambda _: None)

    # False on both attempts: the pre-existing file and the first attempt's.
    assert seen == [False, False]


def test_run_reset_paths_tolerates_a_missing_file(tmp_path):
    fake, _ = _sequence(_envelope(result="fine"))
    assert claude_cli.run("hi", reset_paths=(tmp_path / "never-written.json",),
                          _run=fake) == "fine"


def test_run_reports_why_the_run_ended_not_its_subtype():
    """subtype stays "success" for a run that died in the API."""
    fake, _ = _sequence(API_ERROR)

    with pytest.raises(RuntimeError, match=r"failed \(api_error\)"):
        claude_cli.run("hi", retries=1, _run=fake, _sleep=lambda _: None)


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
    # Phase 2 has no sandboxed workspace and an untrusted prompt: no tools.
    assert kwargs.get("tools", claude_cli.NO_TOOLS) == claude_cli.NO_TOOLS
    assert "you are a claim extractor" in kwargs["system"]
    # Phase 2 has no workspace, so a tool call is a wasted turn.
    assert claude_cli.NO_TOOLS_HINT in kwargs["system"]
    assert kwargs["model"] == "sonnet"
