import subprocess
from pathlib import Path

import pytest

from src import agy_cli


def test_build_argv_includes_model_effort_and_permissions(tmp_path):
    argv = agy_cli.build_argv(prompt="hi", model="gemini-3.8-flash",
                              cwd=tmp_path / "workspace")
    assert argv[0] == "agy"
    assert "--dangerously-skip-permissions" in argv
    assert ["--model", "gemini-3.8-flash"] == argv[argv.index("--model"):argv.index("--model") + 2]
    # gemini-3.8-flash requires effort
    assert ["--effort", "high"] == argv[argv.index("--effort"):argv.index("--effort") + 2]
    assert ["--print", "hi"] == argv[argv.index("--print"):argv.index("--print") + 2]


def test_build_argv_with_explicit_effort_suffix_does_not_need_effort_flag():
    argv = agy_cli.build_argv(prompt="hi", model="gemini-3.8-flash-high")
    assert ["--model", "gemini-3.8-flash-high"] == argv[argv.index("--model"):argv.index("--model") + 2]
    assert "--effort" not in argv


def test_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda bin: "/bin/agy" if bin == "agy" else None)
    assert agy_cli.available() is True
    monkeypatch.setattr("shutil.which", lambda bin: None)
    assert agy_cli.available() is False


def test_version(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1.2.7\n", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert agy_cli.version() == "1.2.7"


def test_run_returns_the_stdout_response():
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "response text\n", "")

    assert agy_cli.run("hi", _run=fake) == "response text"


def test_run_resolves_a_relative_workspace_before_passing_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative_workspace = Path("workspace")
    relative_workspace.mkdir()

    def fake(argv, **kwargs):
        assert kwargs["cwd"] == str(relative_workspace.resolve())
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    assert agy_cli.run("hi", cwd=relative_workspace, _run=fake) == '{"ok": true}'


def test_run_raises_when_binary_not_found():
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "missing", "agy")

    with pytest.raises(RuntimeError, match="agy CLI not found"):
        agy_cli.run("hi", _run=missing)


def test_run_raises_on_non_zero_exit():
    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "permission denied")

    with pytest.raises(RuntimeError, match="agy failed \\(exit 1\\): permission denied"):
        agy_cli.run("hi", _run=failed)


def test_run_raises_on_timeout():
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("agy", 5)

    with pytest.raises(RuntimeError, match="agy timed out after 5s"):
        agy_cli.run("hi", timeout=5, _run=timed_out)


def test_chat_formats_system_and_user_messages(monkeypatch):
    recorded = {}

    def fake_run(prompt, **kwargs):
        recorded["prompt"] = prompt
        recorded["kwargs"] = kwargs
        return '{"result": 123}'

    monkeypatch.setattr(agy_cli, "run", fake_run)
    messages = [
        {"role": "system", "content": "You are a reviewer."},
        {"role": "user", "content": "Check this PR."},
    ]
    res = agy_cli.chat(messages, model="gemini-3.8-flash")
    assert res == '{"result": 123}'
    assert "You are a reviewer." in recorded["prompt"]
    assert "Check this PR." in recorded["prompt"]
    assert recorded["kwargs"]["model"] == "gemini-3.8-flash"
