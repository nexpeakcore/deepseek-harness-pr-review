# tests/test_review_proc.py
import os
import signal
import subprocess
import sys
import time

import pytest

from src import review_budget
from src.review_proc import (EXIT_INCOMPLETE, EXIT_SPAWN_FAILED, EXIT_TIMEOUT,
                             build_argv, run_review)


def test_build_argv_flags():
    assert build_argv("o", "r", 7) == ["o/r", "7", "--force", "--skip-human"]
    assert build_argv("o", "r", 7, force=False, skip_human=False,
                      no_post=True) == ["o/r", "7", "--no-post"]


class _FakePopen:
    """Stands in for the review process: records how it was started."""
    started = []

    def __init__(self, cmd, **kw):
        self.cmd, self.kw = cmd, kw
        self.pid = 4242
        self.returncode = None
        self._timeouts = list(getattr(type(self), "wait_timeouts", []))
        kw["stdout"].write("hello from child\n")
        type(self).started.append(self)

    def wait(self, timeout=None):
        if self._timeouts:
            self._timeouts.pop(0)
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.returncode = getattr(type(self), "exit_code", 0)
        return self.returncode

    def poll(self):
        return self.returncode


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.started = []
    _FakePopen.wait_timeouts = []
    _FakePopen.exit_code = 0
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_run_review_spawns_subprocess_and_writes_log(tmp_path, fake_popen):
    """Log đi vào file của PR; sys.stdout của tiến trình cha không bị đụng."""
    log = tmp_path / "pr-7" / "review.log"
    before = sys.stdout
    code = run_review("o", "r", 7, session_root=tmp_path / "sessions",
                      log_path=log)
    proc = fake_popen.started[0]
    assert code == 0
    assert sys.stdout is before  # không redirect global
    assert log.read_text() == "hello from child\n"
    assert proc.cmd[:3] == [sys.executable, "-m", "src.run"]
    assert proc.cmd[3:] == ["o/r", "7", "--force", "--skip-human"]
    assert proc.kw["env"]["DSH_SESSION_ROOT"] == str(tmp_path / "sessions")
    assert proc.kw["stdin"] == subprocess.DEVNULL  # không có TTY để hỏi human gate
    assert review_budget.DEADLINE_ENV not in proc.kw["env"]


def test_run_review_returns_the_childs_exit_code(tmp_path, fake_popen):
    fake_popen.exit_code = EXIT_INCOMPLETE
    assert run_review("o", "r", 7, session_root=tmp_path,
                      log_path=tmp_path / "l.log") == EXIT_INCOMPLETE


def test_run_review_timeout_kills_the_whole_process_tree(tmp_path, fake_popen,
                                                         monkeypatch):
    """subprocess.run's timeout kills only the child; its agents and git
    processes used to live on, holding index.lock and burning model calls."""
    fake_popen.wait_timeouts = [True]  # first wait() times out, the next returns
    killed = []
    monkeypatch.setattr("src.review_proc.find_process_children",
                        lambda pid: [501, 502])
    monkeypatch.setattr("src.review_proc.pid_alive", lambda pid: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    log = tmp_path / "review.log"
    assert run_review("o", "r", 7, session_root=tmp_path, log_path=log,
                      timeout_seconds=1) == EXIT_TIMEOUT
    assert "timed out" in log.read_text()
    termed = {pid for pid, sig in killed if sig == signal.SIGTERM}
    assert termed == {501, 502, 4242}


def test_run_review_spawn_failure_is_reported(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no such interpreter")

    monkeypatch.setattr(subprocess, "Popen", boom)
    log = tmp_path / "review.log"
    assert run_review("o", "r", 7, session_root=tmp_path,
                      log_path=log) == EXIT_SPAWN_FAILED
    assert "could not start review process" in log.read_text()


def test_soft_deadline_is_set_before_the_kill_timeout(tmp_path, fake_popen):
    before = time.time()
    run_review("o", "r", 7, session_root=tmp_path, log_path=tmp_path / "l.log",
               timeout_seconds=1800, soft_deadline=True)
    deadline = float(fake_popen.started[0].kw["env"][review_budget.DEADLINE_ENV])
    assert before + 1800 - 121 <= deadline <= time.time() + 1800 - 119


def test_no_soft_deadline_unless_asked(tmp_path, fake_popen, monkeypatch):
    # An inherited deadline from a parent must not leak into a manual run.
    monkeypatch.setenv(review_budget.DEADLINE_ENV, "123")
    run_review("o", "r", 7, session_root=tmp_path, log_path=tmp_path / "l.log",
               timeout_seconds=1800)
    assert review_budget.DEADLINE_ENV not in fake_popen.started[0].kw["env"]


def test_resume_adds_the_flag_after_force():
    assert build_argv("o", "r", 7, resume=True) == [
        "o/r", "7", "--force", "--resume", "--skip-human"]


def test_build_argv_no_ping():
    assert "--no-ping" not in build_argv("o", "r", 7)
    assert "--no-ping" in build_argv("o", "r", 7, no_ping=True)


def test_pid_alive():
    import os
    from src.review_proc import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(999999) is False


def test_format_elapsed():
    from src.review_proc import format_elapsed

    assert format_elapsed(None) == "—"
    assert format_elapsed(-5) == "—"
    assert format_elapsed(45) == "45s"
    assert format_elapsed(125) == "2m 05s"
    assert format_elapsed(3665) == "1h 01m"


def test_list_running_processes_empty(tmp_path):
    from src.review_proc import list_running_processes

    assert list_running_processes(tmp_path / "sessions") == []


def test_list_running_processes_finds_reviews_and_autoreview(tmp_path, monkeypatch):
    import json
    import os
    from src.review_proc import list_running_processes

    sessions = tmp_path / "sessions"
    lock1 = sessions / "acme" / "frontend" / "pr-42" / "review.lock"
    lock1.parent.mkdir(parents=True)
    lock1.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-09-29T10:00:00"}))
    log1 = lock1.parent / "review.log"
    log1.write_text("[1/5] snapshot\n[2/5] claims\n[3/5] workspace\n")

    ar_lock = tmp_path / "autoreview.lock"
    ar_lock.write_text(str(os.getpid()))
    ar_log = tmp_path / "autoreview.log"
    ar_log.write_text("[autoreview] running pass\n")

    procs = list_running_processes(sessions, autoreview_lock=ar_lock)
    assert len(procs) == 2

    review_proc = next(p for p in procs if p["type"] == "review")
    assert review_proc["target"] == "acme/frontend#42"
    assert review_proc["pid"] == os.getpid()
    assert review_proc["owner"] == "acme"
    assert review_proc["repo"] == "frontend"
    assert review_proc["pr"] == 42
    assert review_proc["last_log"] == "[3/5] workspace"

    ar_proc = next(p for p in procs if p["type"] == "autoreview")
    assert ar_proc["target"] == "daemon"
    assert ar_proc["pid"] == os.getpid()
    assert ar_proc["last_log"] == "[autoreview] running pass"


def test_list_running_processes_ignores_stale_locks(tmp_path):
    import json
    from src.review_proc import list_running_processes

    sessions = tmp_path / "sessions"
    lock = sessions / "acme" / "frontend" / "pr-42" / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 999999, "started_at": "2026-09-29T10:00:00"}))

    ar_lock = tmp_path / "autoreview.lock"
    ar_lock.write_text("999999")

    procs = list_running_processes(sessions, autoreview_lock=ar_lock)
    assert procs == []


def test_stop_review(tmp_path, monkeypatch):
    import json
    from src.review_proc import stop_review

    sessions = tmp_path / "sessions"

    # 1. Not running
    res = stop_review(sessions, "acme", "frontend", 42)
    assert res["ok"] is False
    assert "No review running" in res["message"]

    # 2. Stale lock
    lock = sessions / "acme" / "frontend" / "pr-42" / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 999999, "started_at": "2026-09-29T10:00:00"}))
    res = stop_review(sessions, "acme", "frontend", 42)
    assert res["ok"] is True
    assert res.get("stale") is True
    assert not lock.exists()

    # 3. Running review
    lock.write_text(json.dumps({"pid": 12345, "started_at": "2026-09-29T10:00:00"}))
    killed_pids = []
    monkeypatch.setattr("src.review_proc.pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr("src.review_proc.stop_process_by_pid", lambda pid: killed_pids.append(pid) or True)

    res = stop_review(sessions, "acme", "frontend", 42)
    assert res["ok"] is True
    assert res["pid"] == 12345
    assert killed_pids == [12345]
    assert not lock.exists()
    log_file = lock.parent / "review.log"
    assert "stopped by user" in log_file.read_text()


def test_stop_autoreview(tmp_path, monkeypatch):
    from src.review_proc import stop_autoreview

    ar_lock = tmp_path / "autoreview.lock"

    # 1. Not running
    res = stop_autoreview(ar_lock)
    assert res["ok"] is False

    # 2. Stale lock
    ar_lock.write_text("999999")
    res = stop_autoreview(ar_lock)
    assert res["ok"] is True
    assert res.get("stale") is True
    assert not ar_lock.exists()

    # 3. Running
    ar_lock.write_text("54321")
    killed = []
    monkeypatch.setattr("src.review_proc.pid_alive", lambda pid: pid == 54321)
    monkeypatch.setattr("src.review_proc.stop_process_by_pid", lambda pid: killed.append(pid) or True)

    res = stop_autoreview(ar_lock)
    assert res["ok"] is True
    assert res["pid"] == 54321
    assert killed == [54321]
    assert not ar_lock.exists()


def test_stop_by_pid(tmp_path, monkeypatch):
    import json
    from src.review_proc import stop_by_pid

    sessions = tmp_path / "sessions"
    lock = sessions / "acme" / "frontend" / "pr-42" / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 11111, "started_at": "2026-09-29T10:00:00"}))

    stopped_reviews = []
    monkeypatch.setattr("src.review_proc.stop_review",
                        lambda s, o, r, p: stopped_reviews.append((o, r, p)) or {"ok": True, "message": "stopped"})

    res = stop_by_pid(sessions, 11111)
    assert res["ok"] is True
    assert stopped_reviews == [("acme", "frontend", 42)]


def test_stop_all_processes(tmp_path, monkeypatch):
    from src.review_proc import stop_all_processes

    procs = [
        {"type": "review", "owner": "o1", "repo": "r1", "pr": 1, "pid": 101},
        {"type": "autoreview", "lock_path": tmp_path / "autoreview.lock", "pid": 102},
    ]
    monkeypatch.setattr("src.review_proc.list_running_processes", lambda *a, **k: procs)

    stopped = []
    monkeypatch.setattr("src.review_proc.stop_review",
                        lambda s, o, r, p: stopped.append(f"review {o}/{r}#{p}") or {"ok": True, "message": f"review {o}/{r}#{p}"})
    monkeypatch.setattr("src.review_proc.stop_autoreview",
                        lambda l: stopped.append("autoreview") or {"ok": True, "message": "autoreview"})

    results = stop_all_processes(tmp_path)
    assert len(results) == 2
    assert stopped == ["review o1/r1#1", "autoreview"]



def test_kill_tree_stops_real_grandchildren(tmp_path):
    """Not a mock: a review whose agents ignore its death must not outlive it."""
    from src.review_proc import _kill_tree, find_process_children, pid_alive

    proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"])
    try:
        grandchildren = []
        for _ in range(50):  # the shell forks them a moment after it starts
            grandchildren = find_process_children(proc.pid)
            if len(grandchildren) >= 2:
                break
            time.sleep(0.1)
        assert len(grandchildren) >= 2

        _kill_tree(proc, grace=2)

        assert proc.poll() is not None
        for _ in range(50):
            if not any(pid_alive(p) for p in grandchildren):
                break
            time.sleep(0.1)
        assert not any(pid_alive(p) for p in grandchildren)
    finally:
        if proc.poll() is None:
            proc.kill()
