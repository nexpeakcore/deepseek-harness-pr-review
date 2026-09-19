import subprocess

import pytest

from src import codex_cli


def test_build_argv_is_read_only_and_ignores_local_rules(tmp_path):
    argv = codex_cli.build_argv(model="gpt-5.5", output_path=tmp_path / "out",
                                cwd=tmp_path / "workspace")
    assert argv[:2] == ["codex", "exec"]
    assert ["--sandbox", "read-only"] == argv[argv.index("--sandbox"):argv.index("--sandbox") + 2]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv


def test_run_returns_the_final_message(tmp_path):
    def fake(argv, **kwargs):
        Path = __import__("pathlib").Path
        Path(argv[argv.index("--output-last-message") + 1]).write_text('{"ok": true}')
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert codex_cli.run("hi", _run=fake) == '{"ok": true}'


def test_run_names_a_missing_binary():
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "missing", "codex")

    with pytest.raises(RuntimeError, match="codex CLI not found"):
        codex_cli.run("hi", _run=missing)
