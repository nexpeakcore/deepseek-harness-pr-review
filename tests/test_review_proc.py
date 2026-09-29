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

