# tests/test_review_proc.py
import subprocess
import sys

from src.review_proc import EXIT_SPAWN_FAILED, EXIT_TIMEOUT, build_argv, run_review


def test_build_argv_flags():
    assert build_argv("o", "r", 7) == ["o/r", "7", "--force", "--skip-human"]
    assert build_argv("o", "r", 7, force=False, skip_human=False,
                      no_post=True) == ["o/r", "7", "--no-post"]


def test_run_review_spawns_subprocess_and_writes_log(tmp_path, monkeypatch):
    """Log đi vào file của PR; sys.stdout của tiến trình cha không bị đụng."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        seen["stdin"] = kw["stdin"]
        kw["stdout"].write("hello from child\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    log = tmp_path / "pr-7" / "review.log"
    before = sys.stdout
    code = run_review("o", "r", 7, session_root=tmp_path / "sessions",
                      log_path=log)
    assert code == 0
    assert sys.stdout is before  # không redirect global
    assert log.read_text() == "hello from child\n"
    assert seen["cmd"][:3] == [sys.executable, "-m", "src.run"]
    assert seen["cmd"][3:] == ["o/r", "7", "--force", "--skip-human"]
    assert seen["env"]["DSH_SESSION_ROOT"] == str(tmp_path / "sessions")
    assert seen["stdin"] == subprocess.DEVNULL  # không có TTY để hỏi human gate


def test_run_review_timeout_is_reported(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    log = tmp_path / "review.log"
    assert run_review("o", "r", 7, session_root=tmp_path, log_path=log,
                      timeout_seconds=1) == EXIT_TIMEOUT
    assert "timed out" in log.read_text()


def test_run_review_spawn_failure_is_reported(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise OSError("no such interpreter")

    monkeypatch.setattr(subprocess, "run", fake_run)
    log = tmp_path / "review.log"
    assert run_review("o", "r", 7, session_root=tmp_path,
                      log_path=log) == EXIT_SPAWN_FAILED
    assert "could not start review process" in log.read_text()
