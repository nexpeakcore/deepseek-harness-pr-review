import json

from src.run import main

FIXTURES = {
    "snapshot.json": {
        "owner": "demo", "repo": "app", "pr": 7, "title": "T",
        "body": "B", "author": "a", "base": "main", "head": "x",
        "labels": [], "files": [], "commits": [], "threads": [],
    },
    "claims.json": [],
    "findings.json": {
        "claims": [], "docs": [], "impact": [], "threads": [],
        "unresolved_questions": [],
    },
}


def test_main_fixtures_mode(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name, data in FIXTURES.items():
        (fixtures / name).write_text(json.dumps(data))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))

    code = main(["demo/app", "7", "--fixtures", str(fixtures), "--no-post"])
    assert code == 0
    report = tmp_path / "sessions" / "demo" / "app" / "pr-7" / "report.md"
    assert report.exists()


def test_main_requires_gh(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr("src.run.gh_available", lambda: False)
    code = main(["demo/app", "7", "--no-post"])
    assert code == 2


def test_main_owner_repo_hash_parsing(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name, data in FIXTURES.items():
        (fixtures / name).write_text(json.dumps(data))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    code = main(["demo/app#7", "--fixtures", str(fixtures), "--no-post"])
    assert code == 0
    assert (tmp_path / "sessions" / "demo" / "app" / "pr-7" / "report.md").exists()


def test_rerun_skips_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    calls = {"verify": 0}
    fake_snapshot = {"owner": "demo", "repo": "app", "pr": 7, "title": "T",
                     "body": "B", "author": "a", "base": "main", "head": "x",
                     "labels": [], "files": [], "commits": [], "threads": []}
    fake_claims = []
    fake_findings = {"claims": [], "docs": [], "impact": [], "threads": [],
                     "unresolved_questions": []}
    fake_setup_calls = []

    def fake_build_snapshot(owner, repo, n, session_dir, gh=None):
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshot.json").write_text(json.dumps(fake_snapshot))
        return fake_snapshot

    def fake_extract_claims(snapshot, cfg, session_dir, chat=None):
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "claims.json").write_text(json.dumps(fake_claims))
        return fake_claims

    def fake_setup_workspace(owner, repo, n, workspace, remote_url=None):
        fake_setup_calls.append(1)

    def fake_run_verify(cfg, workspace, session_dir, snapshot, claims):
        calls["verify"] += 1
        return fake_findings

    monkeypatch.setattr("src.snapshot.build_snapshot", fake_build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", fake_extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", fake_setup_workspace)
    monkeypatch.setattr("src.run.run_verify", fake_run_verify)

    code1 = main(["demo/app", "7", "--no-post"])
    assert code1 == 0
    assert calls["verify"] == 1
    assert len(fake_setup_calls) == 1
    assert (tmp_path / "sessions" / "demo" / "app" / "pr-7" / "findings.json").exists()

    code2 = main(["demo/app", "7", "--no-post"])
    assert code2 == 0
    assert calls["verify"] == 1
    assert len(fake_setup_calls) == 1


def test_verify_run_bumps_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)

    fake_snapshot = {"owner": "demo", "repo": "app", "pr": 7, "title": "T",
                     "body": "B", "author": "a", "base": "main", "head": "x",
                     "labels": [], "files": [], "commits": [], "threads": []}
    fake_findings = {"claims": [], "docs": [], "impact": [], "threads": [],
                     "unresolved_questions": []}

    def fake_build_snapshot(owner, repo, n, session_dir, gh=None):
        (session_dir / "snapshot.json").write_text(json.dumps(fake_snapshot))
        return fake_snapshot

    def fake_extract_claims(snapshot, cfg, session_dir, chat=None):
        (session_dir / "claims.json").write_text(json.dumps([]))
        return []

    def fake_setup_workspace(owner, repo, n, workspace, remote_url=None):
        pass

    def fake_run_verify(cfg, workspace, session_dir, snapshot, claims):
        return fake_findings

    monkeypatch.setattr("src.snapshot.build_snapshot", fake_build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", fake_extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", fake_setup_workspace)
    monkeypatch.setattr("src.run.run_verify", fake_run_verify)

    assert main(["demo/app", "7", "--no-post"]) == 0
    rounds_file = tmp_path / "sessions" / "demo" / "app" / "pr-7" / "rounds.txt"
    assert rounds_file.read_text().strip() == "1"

    assert main(["demo/app", "7", "--no-post"]) == 0  # cached run, no bump
    assert rounds_file.read_text().strip() == "1"

    assert main(["demo/app", "7", "--no-post", "--force"]) == 0  # force → bump
    assert rounds_file.read_text().strip() == "2"


def test_main_creates_and_releases_review_lock(tmp_path, monkeypatch):
    """Real run → review.lock được tạo (ngăn poller trùng) và luôn được giải phóng."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    fake_snapshot = {"owner": "demo", "repo": "app", "pr": 7, "title": "T",
                     "body": "B", "author": "a", "base": "main", "head": "x",
                     "labels": [], "files": [], "commits": [], "threads": []}
    fake_findings = {"claims": [], "docs": [], "impact": [], "threads": [],
                     "unresolved_questions": []}

    def fake_build_snapshot(owner, repo, n, session_dir, gh=None):
        (session_dir / "snapshot.json").write_text(json.dumps(fake_snapshot))
        return fake_snapshot

    def fake_extract_claims(snapshot, cfg, session_dir, chat=None):
        (session_dir / "claims.json").write_text(json.dumps([]))
        return []

    def fake_run_verify(cfg, workspace, session_dir, snapshot, claims):
        # Trong lúc verify, lock phải tồn tại
        assert (session_dir / "review.lock").exists()
        return fake_findings

    monkeypatch.setattr("src.snapshot.build_snapshot", fake_build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", fake_extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", lambda *a, **k: None)
    monkeypatch.setattr("src.run.run_verify", fake_run_verify)

    session_dir = tmp_path / "sessions" / "demo" / "app" / "pr-7"
    assert main(["demo/app", "7", "--no-post"]) == 0
    # Lock đã được giải phóng sau run
    assert not (session_dir / "review.lock").exists()


def test_main_refuses_when_review_lock_held(tmp_path, monkeypatch):
    """Review khác đang chạy (lock sống) → từ chối, không chạy pipeline."""
    import os

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    session_dir = tmp_path / "sessions" / "demo" / "app" / "pr-7"
    session_dir.mkdir(parents=True)
    (session_dir / "review.lock").write_text(
        json.dumps({"pid": os.getpid(),
                    "started_at": "2026-08-17T00:00:00"}))

    called = {"verify": 0}

    def fake_run_verify(*a, **k):
        called["verify"] += 1
        return {"claims": [], "docs": [], "impact": [], "threads": [],
                "unresolved_questions": []}

    monkeypatch.setattr("src.snapshot.build_snapshot",
                        lambda *a, **k: {"pr": 7, "title": "T"})
    monkeypatch.setattr("src.claims.extract_claims", lambda *a, **k: [])
    monkeypatch.setattr("src.run.run_verify", fake_run_verify)

    assert main(["demo/app", "7", "--no-post"]) == 1
    assert called["verify"] == 0


def test_fixtures_mode_skips_review_lock(tmp_path, monkeypatch):
    """Fixtures mode không chạy verify → không tạo lock."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name, data in FIXTURES.items():
        (fixtures / name).write_text(json.dumps(data))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))

    assert main(["demo/app", "7", "--fixtures", str(fixtures),
                 "--no-post"]) == 0
    assert not (tmp_path / "sessions" / "demo" / "app" / "pr-7"
                / "review.lock").exists()


def test_doctor_ready(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("src.run.run_gh", lambda args, **kw: {"login": "dev1"})

    code = main(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Python 3.10+" in out
    assert "dev1" in out
    assert "DEEPSEEK_API_KEY set" in out
    assert "Ready" in out


def test_doctor_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("src.run.run_gh", lambda args, **kw: {"login": "dev1"})

    code = main(["doctor"])
    assert code == 1
    out = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY not set" in out


def test_doctor_no_gh(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr("src.run.gh_available", lambda: False)

    code = main(["doctor"])
    assert code == 1
    out = capsys.readouterr().out
    assert "gh CLI not installed" in out


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr("run.importlib.metadata.version",
                        lambda name: "0.1.0")
    code = main(["--version"])
    assert code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_update_command(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("run.importlib.metadata.version",
                        lambda name: "0.1.0")

    def fake_pip(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("subprocess.run", fake_pip)
    code = main(["update"])
    assert code == 0
    assert calls[0][0] == "python" or "pip" in str(calls[0])
    assert "install" in calls[0]
    assert "deepseek-harness-pr-review[web] @ git+https://github.com/nexpeakcore/deepseek-harness-pr-review.git" in " ".join(calls[0])
    assert "Updated" in capsys.readouterr().out


def test_web_command_starts_uvicorn(monkeypatch):
    started = {}
    import sys

    fake_uvicorn = type("U", (), {
        "run": lambda app, host, port: started.update(
            {"host": host, "port": port})})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    code = main(["web"])
    assert code == 0
    assert started == {"host": "127.0.0.1", "port": 6789}


def test_web_command_missing_uvicorn(monkeypatch, capsys):
    import sys as _sys

    class NoUvicorn:
        def find_module(self, name, path=None):
            if name == "uvicorn":
                raise ImportError
            return None

    monkeypatch.setitem(_sys.modules, "uvicorn", None)
    monkeypatch.delitem(_sys.modules, "uvicorn", raising=False)
    # force import failure bằng cách xóa khỏi sys.modules và chặn
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ModuleNotFoundError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    code = main(["web"])
    assert code == 1
    assert "web" in capsys.readouterr().err


def test_main_reclaims_stale_review_lock(tmp_path, monkeypatch):
    """Lock của review đã crash (PID chết) → thu hồi, không kẹt PR vĩnh viễn."""
    import os

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    session_dir = tmp_path / "sessions" / "demo" / "app" / "pr-7"
    session_dir.mkdir(parents=True)
    dead = os.fork()
    if dead == 0:
        os._exit(0)
    os.waitpid(dead, 0)  # PID chắc chắn đã chết
    (session_dir / "review.lock").write_text(
        json.dumps({"pid": dead, "started_at": "2026-08-17T00:00:00"}))

    fake_snapshot = {"owner": "demo", "repo": "app", "pr": 7, "title": "T",
                     "body": "B", "author": "a", "base": "main", "head": "x",
                     "labels": [], "files": [], "commits": [], "threads": []}
    fake_findings = {"claims": [], "docs": [], "impact": [], "threads": [],
                     "unresolved_questions": []}

    def fake_build_snapshot(owner, repo, n, session_dir, gh=None):
        (session_dir / "snapshot.json").write_text(json.dumps(fake_snapshot))
        return fake_snapshot

    def fake_extract_claims(snapshot, cfg, session_dir, chat=None):
        (session_dir / "claims.json").write_text(json.dumps([]))
        return []

    monkeypatch.setattr("src.snapshot.build_snapshot", fake_build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", fake_extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", lambda *a, **k: None)
    monkeypatch.setattr("src.run.run_verify",
                        lambda *a, **k: fake_findings)

    assert main(["demo/app", "7", "--no-post"]) == 0
    assert not (session_dir / "review.lock").exists()


def test_review_lock_corrupt_is_treated_as_stale(tmp_path):
    """review.lock rác (truncate/ghi dở) → coi là stale, không kẹt."""
    from src.run import _acquire_review_lock, _release_review_lock

    session_dir = tmp_path / "pr-7"
    session_dir.mkdir()
    (session_dir / "review.lock").write_text("not json at all")
    assert _acquire_review_lock(session_dir) is True
    _release_review_lock(session_dir)


def _stub_pipeline(monkeypatch, tmp_path, findings=None):
    """Chạy main() tới bước post mà không gọi gh/model thật."""
    findings = findings or {
        "claims": [{"id": "C1", "status": "PARTIAL", "evidence": [], "note": ""}],
        "docs": [{"path": "README.md", "status": "STALE", "what": "x"}],
        "impact": [], "threads": [], "unresolved_questions": []}
    snapshot = {"owner": "demo", "repo": "app", "pr": 7, "title": "T",
                "body": "B", "author": "a", "base": "main", "head": "x",
                "head_sha": "abcdef1234", "labels": [], "files": [],
                "commits": [], "threads": []}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.run.gh_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    def fake_build_snapshot(owner, repo, n, session_dir, gh=None):
        (session_dir / "snapshot.json").write_text(json.dumps(snapshot))
        return snapshot

    def fake_extract_claims(snapshot, cfg, session_dir, chat=None):
        (session_dir / "claims.json").write_text(json.dumps([]))
        return []

    monkeypatch.setattr("src.snapshot.build_snapshot", fake_build_snapshot)
    monkeypatch.setattr("src.claims.extract_claims", fake_extract_claims)
    monkeypatch.setattr("src.run.setup_workspace", lambda *a, **k: None)
    monkeypatch.setattr("src.run.run_verify", lambda *a, **k: findings)
    return snapshot, findings


def test_main_posts_report_and_round_ping(tmp_path, monkeypatch, capsys):
    """Mỗi vòng: báo cáo sửa tại chỗ + 1 comment ngắn MỚI để có thông báo.

    Patch ở seam của run.py chứ không patch src.synthesize.run_gh: post_comment
    bind gh=run_gh làm default lúc import nên patch module không ăn.
    """
    snapshot, findings = _stub_pipeline(monkeypatch, tmp_path)
    calls = {}

    monkeypatch.setattr("src.run.post_comment",
                        lambda o, r, n, body: calls.setdefault("report", body) or True)
    monkeypatch.setattr("src.run.find_report_comment",
                        lambda o, r, n: {"html_url": "https://gh/c/1"})
    monkeypatch.setattr("src.run.post_ping",
                        lambda o, r, n, body: calls.setdefault("ping", body))

    assert main(["demo/app", "7", "--skip-human"]) == 0
    assert "report" in calls and "ping" in calls
    ping = calls["ping"]
    assert "Harness review" in ping
    assert "`abcdef1`" in ping                 # commit đã review
    assert "**1** risk" in ping                # 1 claim PARTIAL
    assert "**1** doc error" in ping           # 1 STALE
    assert "https://gh/c/1" in ping            # link tới báo cáo đầy đủ
    assert len(ping) < 400                     # "ngắn" là một yêu cầu
    assert "Posted round ping." in capsys.readouterr().out


def test_main_no_ping_flag_skips_the_ping(tmp_path, monkeypatch, capsys):
    _stub_pipeline(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr("src.run.post_comment", lambda *a, **k: True)
    monkeypatch.setattr("src.run.post_ping",
                        lambda *a, **k: calls.append(a))
    assert main(["demo/app", "7", "--skip-human", "--no-ping"]) == 0
    assert calls == []
    assert "Posted round ping." not in capsys.readouterr().out


def test_ping_failure_does_not_fail_the_review(tmp_path, monkeypatch, capsys):
    """Mất ping thì khó chịu; mất cả review vì ping thì tệ hơn."""
    _stub_pipeline(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("gh api failed: rate limited")

    monkeypatch.setattr("src.run.post_comment", lambda *a, **k: True)
    monkeypatch.setattr("src.run.find_report_comment", boom)

    assert main(["demo/app", "7", "--skip-human"]) == 0
    assert "could not post round ping" in capsys.readouterr().err


def test_ping_still_posts_when_report_comment_url_is_unavailable(tmp_path,
                                                                 monkeypatch):
    """Không tìm được comment báo cáo → ping vẫn phải ra, chỉ thiếu link."""
    _stub_pipeline(monkeypatch, tmp_path)
    calls = {}
    monkeypatch.setattr("src.run.post_comment", lambda *a, **k: True)
    monkeypatch.setattr("src.run.find_report_comment", lambda o, r, n: None)
    monkeypatch.setattr("src.run.post_ping",
                        lambda o, r, n, body: calls.setdefault("ping", body))
    assert main(["demo/app", "7", "--skip-human"]) == 0
    assert "Harness review" in calls["ping"]
    assert "Full report" not in calls["ping"]


def test_review_prints_phase_progress(tmp_path, monkeypatch, capsys):
    """A running review must be distinguishable from a hung one in review.log."""
    import json as _json

    from src.run import main

    session = tmp_path / "demo" / "app" / "pr-7"
    session.mkdir(parents=True)
    (session / "snapshot.json").write_text(_json.dumps(
        {"pr": 7, "owner": "demo", "repo": "app", "title": "t", "author": "a",
         "base": "main", "head": "h", "body": "b",
         "files": [{"filename": "a.py"}], "commits": [{"sha": "x"}],
         "threads": [], "linked_issues": []}))
    (session / "claims.json").write_text(_json.dumps(
        [{"id": "C1", "text": "x", "category": "feature", "source": "stated"}]))
    (session / "findings.json").write_text(_json.dumps(
        {"claims": [{"id": "C1", "status": "PASS", "evidence": [], "note": ""}],
         "docs": [], "impact": [], "threads": [], "unresolved_questions": []}))

    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr("src.run.gh_available", lambda: True)

    assert main(["demo/app", "7", "--skip-human", "--no-post"]) == 0
    out = capsys.readouterr().out
    assert "[1/5] snapshot — 1 files, 1 commits" in out
    assert "[2/5] claims — 1 claims from description" in out
    assert "[5/5] report — 1 claims, 0 docs, 0 impact" in out
