# tests/test_server.py
import json
import os

import pytest
from fastapi.testclient import TestClient

from src.autoreview_config import load_config
from src.review_proc import build_argv
from web.server import app

EMPTY_FINDINGS = {"claims": [], "docs": [], "impact": [], "threads": [],
                  "unresolved_questions": []}

SNAPSHOT = {"pr": 7, "title": "Add checkout", "author": "dev1",
            "base": "main", "head": "x", "files": [], "commits": [], "threads": []}


def _write_session(root, owner, repo, pr, snapshot=None, findings=None,
                   answers=None, report=None):
    d = root / owner / repo / f"pr-{pr}"
    d.mkdir(parents=True, exist_ok=True)
    if snapshot is not None:
        (d / "snapshot.json").write_text(json.dumps(snapshot))
    if findings is not None:
        (d / "findings.json").write_text(json.dumps(findings))
    if answers is not None:
        (d / "answers.json").write_text(json.dumps(answers or []))
    if report is not None:
        (d / "report.md").write_text(report)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path))
    _write_session(tmp_path, "sample-org", "sample-app", 77,
                   snapshot={**SNAPSHOT, "pr": 77, "title": "Google sign-in"},
                   findings={
                       "claims": [{"id": "C1", "status": "PASS",
                                   "evidence": ["a.dart:1"], "note": ""}],
                       "docs": [{"path": "docs/PLAN.md", "status": "WRONG",
                                 "what": "doc sai"}],
                       "impact": [{"requirement": "Auth", "impact": "CHANGED",
                                   "detail": "d"}],
                       "threads": [{"text": "check validation",
                                    "status": "STILL_VALID", "note": ""}],
                       "unresolved_questions": ["Doc PLAN wrong?"],
                   },
                   answers=[{"question": "Doc PLAN wrong?", "kind": "doc",
                             "answer": "SKIPPED"}])
    return TestClient(app)


def test_repo_list_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "sample-app" in resp.text


def test_repo_list_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path))
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "No reviews yet" in resp.text


def test_repo_page(client, monkeypatch):
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 77, "title": "Google sign-in",
                                             "draft": False}])
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "Google sign-in" in resp.text
    assert "PRs REVIEWED" in resp.text


def test_pr_page_tabs(client):
    resp = client.get("/repos/sample-org/sample-app/pr/77")
    assert resp.status_code == 200
    assert "Claims" in resp.text
    assert "Docs" in resp.text
    assert "STILL_VALID" in resp.text
    assert "SKIPPED" in resp.text


def test_unknown_repo_404(client):
    assert client.get("/repos/sample-org/nope").status_code == 404


def test_unknown_pr_404(client):
    assert client.get("/repos/sample-org/sample-app/pr/999").status_code == 404


def test_api_config_and_toggle(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text(
        "org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))

    def fake_gh(args, **kw):
        return [{"name": "sample-app"}, {"name": "admin-web"}]

    monkeypatch.setattr("src.gh.run_gh", fake_gh)

    client = TestClient(app)

    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["org"] == "sample-org"
    by_name = {x["name"]: x["mode"] for x in data["repos"]}
    assert by_name["sample-app"] == "auto"
    assert by_name["admin-web"] == "unlisted"

    r = client.post("/api/config/repos/sample-app/mode",
                    json={"mode": "manual"})
    assert r.status_code == 200
    assert load_config(cfg_path)["repos"]["sample-app"] == "manual"

    r = client.post("/api/config/repos", json={"repo": "payments"})
    assert r.status_code == 200
    assert load_config(cfg_path)["repos"]["payments"] == "auto"

    r = client.delete("/api/config/repos/payments")
    assert r.status_code == 200
    assert "payments" not in load_config(cfg_path)["repos"]


def test_api_add_repo_without_org_rejects_name(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("repos:\n  sample-app: auto\n")  # no org
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    client = TestClient(app)
    r = client.post("/api/config/repos", json={"repo": "payments"})
    assert r.status_code == 400
    assert "org" in r.json()["detail"]


def test_api_toggle_bad_mode_400(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    client = TestClient(app)
    r = client.post("/api/config/repos/sample-app/mode", json={"mode": "x"})
    assert r.status_code == 400


def test_api_config_missing_file_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(tmp_path / "none.yml"))
    client = TestClient(app)
    assert client.get("/api/config").status_code == 404


def test_config_page_has_config_block(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh", lambda args, **kw: [{"name": "sample-app"}])
    client = TestClient(app)
    r = client.get("/config")
    assert r.status_code == 200
    assert "Repo configuration" in r.text
    assert "sample-app" in r.text


def test_repo_list_page_has_no_config_block(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh", lambda args, **kw: [{"name": "sample-app"}])
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "Repo configuration" not in r.text
    assert "Config" in r.text  # navbar link


def test_repo_page_shows_open_prs(client, tmp_path, monkeypatch):
    def fake_gh(args, **kw):
        return [
            {"number": 78, "title": "chore: update deps", "draft": False},
            {"number": 77, "title": "Google sign-in", "draft": False},
        ]

    monkeypatch.setattr("src.gh.run_gh", fake_gh)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "chore: update deps" in resp.text
    assert "Not reviewed" in resp.text
    assert "Reviewed" in resp.text


def test_repo_page_gh_failure_badge(client, tmp_path, monkeypatch):
    def fake_gh(args, **kw):
        raise RuntimeError("rate limited")

    monkeypatch.setattr("src.gh.run_gh", fake_gh)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "open PRs unavailable" in resp.text


def test_trigger_review_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    calls = []

    def fake_review(owner, repo, n, **kw):
        calls.append((owner, repo, n, kw))
        return 0

    monkeypatch.setattr("web.server.run_review", fake_review)
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    owner, repo, n, kw = calls[0]
    args = build_argv(owner, repo, n, force=kw["force"],
                      skip_human=kw["skip_human"], no_post=kw["no_post"])
    assert "sample-org/sample-app" in args and "78" in args
    assert "--force" in args
    assert "--skip-human" in args       # config mặc định skip_human: true
    assert "--no-post" not in args      # config mặc định post_comment: true
    # log đi thẳng vào file của PR, không đụng sys.stdout của server
    assert kw["log_path"].name == "review.log"
    assert kw["log_path"].parent.name == "pr-78"


def test_trigger_review_no_post_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n"
                        "post_comment: false\nskip_human: false\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    calls = []

    def fake_review(owner, repo, n, **kw):
        calls.append((owner, repo, n, kw))
        return 0

    monkeypatch.setattr("web.server.run_review", fake_review)
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 200
    owner, repo, n, kw = calls[0]
    args = build_argv(owner, repo, n, force=kw["force"],
                      skip_human=kw["skip_human"], no_post=kw["no_post"])
    assert "--no-post" in args
    assert "--skip-human" not in args


def test_trigger_review_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setattr("web.server.run_review", lambda *a, **k: 3)  # thiếu API key
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 400
    assert "DEEPSEEK_API_KEY" in r.json()["detail"]


def test_trigger_review_error_500(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setattr("web.server.run_review", lambda *a, **k: 2)  # gh lỗi
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 500
    assert "review failed" in r.json()["detail"]


def test_trigger_review_concurrent_409(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("web.server.run_review", lambda *a, **k: 0)
    lock = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78" \
        / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-08-16T10:00:00"}))
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]
    assert "PID" in r.json()["detail"]


def test_review_status_api_running(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    lock = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78" \
        / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-08-16T10:00:00"}))
    client = TestClient(app)
    r = client.get("/api/repos/sample-org/sample-app/pr/78/review/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is True
    assert data["pid"] == os.getpid()


def test_review_status_api_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    lock = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78" \
        / "review.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 999999, "started_at": "2026-08-16T10:00:00"}))
    client = TestClient(app)
    r = client.get("/api/repos/sample-org/sample-app/pr/78/review/status")
    assert r.status_code == 200
    assert r.json()["running"] is False
    assert r.json()["stale"] is True


def test_review_log_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    session_dir = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78"
    session_dir.mkdir(parents=True)
    (session_dir / "review.log").write_text("line1\nline2\nline3\n")
    client = TestClient(app)
    r = client.get("/api/repos/sample-org/sample-app/pr/78/review/log?lines=2")
    assert r.status_code == 200
    data = r.json()
    assert data["log"] == "line2\nline3"
    assert data["running"] is False


def test_review_log_api_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    client = TestClient(app)
    r = client.get("/api/repos/sample-org/sample-app/pr/78/review/log")
    assert r.status_code == 200
    assert r.json() == {"log": "", "running": False}


def test_repo_page_has_review_buttons(client, monkeypatch):
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [
                            {"number": 78, "title": "chore: update deps",
                             "draft": False}])
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "Review now" in resp.text


def test_repo_list_shows_auto_repos_without_data(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "sample-org/sample-app" in r.text
    assert "AUTO" in r.text
    assert "reviewed automatically" in r.text


def test_pr_page_not_reviewed_placeholder(client, monkeypatch):
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: {"number": 78,
                                            "title": "chore: update deps",
                                            "user": {"login": "bot"},
                                            "base": {"ref": "main"},
                                            "head": {"ref": "renovate"}})
    resp = client.get("/repos/sample-org/sample-app/pr/78")
    assert resp.status_code == 200
    assert "Not reviewed yet" in resp.text
    assert "Review now" in resp.text
    assert "chore: update deps" in resp.text


def test_repo_page_no_session_shows_open_prs(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 78,
                                             "title": "chore: update deps",
                                             "draft": False}] if "pulls" in args[1]
                        else {"full_name": "sample-org/sample-app"})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "No reviews yet" in resp.text
    assert "chore: update deps" in resp.text
    assert "Review now" in resp.text


def test_repo_page_gh_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))

    def fake_gh(args, **kw):
        raise RuntimeError("not found")

    monkeypatch.setattr("src.gh.run_gh", fake_gh)
    client = TestClient(app)
    assert client.get("/repos/sample-org/nope").status_code == 404


def test_repo_list_auto_card_links_dashboard(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/repos/sample-org/sample-app"' in r.text


def test_pr_page_reviewing_state(tmp_path, monkeypatch):
    import os as _os

    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    session_dir = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78"
    session_dir.mkdir(parents=True)
    (session_dir / "snapshot.json").write_text(json.dumps({"pr": 78}))
    (session_dir / "review.lock").write_text(
        json.dumps({"pid": _os.getpid(), "started_at": "2026-08-16T10:00:00"}))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: {"number": 78, "title": "T",
                                            "user": {"login": "a"},
                                            "base": {"ref": "main"},
                                            "head": {"ref": "x"}})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app/pr/78")
    assert resp.status_code == 200
    assert "Reviewing…" in resp.text
    assert "Not reviewed yet" not in resp.text


def test_review_log_api_live_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    session_dir = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78"
    session_dir.mkdir(parents=True)
    (session_dir / "review.log").write_text("line1\nline2\n")
    client = TestClient(app)
    r = client.get("/api/repos/sample-org/sample-app/pr/78/review/log?lines=200")
    assert r.status_code == 200
    assert r.json()["log"] == "line1\nline2"


def test_repo_page_shows_mode_badge(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 78, "title": "T",
                                             "draft": False}] if "pulls" in args[1]
                        else {"full_name": "sample-org/sample-app"})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "AUTO" in resp.text


def test_repo_page_mode_manual(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: manual\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 78, "title": "T",
                                             "draft": False}] if "pulls" in args[1]
                        else {"full_name": "sample-org/sample-app"})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "MANUAL" in resp.text


def test_repo_page_unconfigured_shows_manual_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  other-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 78, "title": "T",
                                             "draft": False}] if "pulls" in args[1]
                        else {"full_name": "sample-org/sample-app"})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "MANUAL (default)" in resp.text
    assert "Switch to AUTO" in resp.text


def test_repo_page_configured_auto_shows_switch_manual(tmp_path, monkeypatch):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: [{"number": 78, "title": "T",
                                             "draft": False}] if "pulls" in args[1]
                        else {"full_name": "sample-org/sample-app"})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app")
    assert resp.status_code == 200
    assert "AUTO" in resp.text
    assert "Switch to MANUAL" in resp.text
    assert "(default)" not in resp.text


def test_pr_page_failed_state_matches_repo_list(tmp_path, monkeypatch):
    """Session dở dang, không lock sống → 'Failed · interrupted', giống repo list."""
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    session_dir = tmp_path / "sessions" / "sample-org" / "sample-app" / "pr-78"
    session_dir.mkdir(parents=True)
    (session_dir / "snapshot.json").write_text(json.dumps({"pr": 78}))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: {"number": 78, "title": "T",
                                            "user": {"login": "a"},
                                            "base": {"ref": "main"},
                                            "head": {"ref": "x"}})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app/pr/78")
    assert resp.status_code == 200
    assert "Failed · interrupted" in resp.text
    assert "Not reviewed yet" not in resp.text


def test_pr_page_never_reviewed_still_says_not_reviewed(tmp_path, monkeypatch):
    """Không có session dir → vẫn là 'Not reviewed yet', không phải failed."""
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh",
                        lambda args, **kw: {"number": 79, "title": "T",
                                            "user": {"login": "a"},
                                            "base": {"ref": "main"},
                                            "head": {"ref": "x"}})
    client = TestClient(app)
    resp = client.get("/repos/sample-org/sample-app/pr/79")
    assert resp.status_code == 200
    assert "Not reviewed yet" in resp.text
    assert "Failed · interrupted" not in resp.text


def _cfg_client(tmp_path, monkeypatch, gh,
                body="org: sample-org\nrepos:\n  sample-app: auto\n"):
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text(body)
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr("src.gh.run_gh", gh)
    # The reachability cache is process-global; tests must not inherit each other.
    import web.server
    web.server._check_cache.clear()
    return TestClient(app), cfg_path

def _gh_with_missing(missing):
    """Org listing works; repos/<x> 404s for names in `missing`."""
    def gh(args, **kw):
        target = args[1]
        if target.startswith("orgs/"):
            return [{"name": "sample-app"}]
        if any(target.endswith(m) for m in missing):
            raise RuntimeError("gh api failed: gh: Not Found (HTTP 404)")
        return target.removeprefix("repos/")
    return gh


def test_add_repo_refuses_one_github_cannot_see(tmp_path, monkeypatch):
    client, cfg_path = _cfg_client(tmp_path, monkeypatch,
                                   _gh_with_missing(["typoed-repo"]))
    r = client.post("/api/config/repos", json={"repo": "typoed-repo"})
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]
    # The whole point: it must not reach the config file.
    assert "typoed-repo" not in load_config(cfg_path)["repos"]


def test_add_repo_accepts_a_reachable_one(tmp_path, monkeypatch):
    client, cfg_path = _cfg_client(tmp_path, monkeypatch, _gh_with_missing([]))
    r = client.post("/api/config/repos", json={"repo": "acme/api"})
    assert r.status_code == 200
    assert r.json()["warning"] is None
    assert load_config(cfg_path)["repos"]["acme/api"] == "auto"


def test_add_repo_survives_an_unverifiable_check(tmp_path, monkeypatch):
    """A network blip must not block a legitimate repo — but must be said out loud."""
    def gh(args, **kw):
        if args[1].startswith("orgs/"):
            return []
        raise RuntimeError("dial tcp: connection refused")

    client, cfg_path = _cfg_client(tmp_path, monkeypatch, gh)
    r = client.post("/api/config/repos", json={"repo": "acme/thing"})
    assert r.status_code == 200
    assert "could not verify" in r.json()["warning"]
    assert load_config(cfg_path)["repos"]["acme/thing"] == "auto"


def test_check_endpoint_lists_unreachable_repos(tmp_path, monkeypatch):
    client, _ = _cfg_client(
        tmp_path, monkeypatch, _gh_with_missing(["ghost", "sample-org/gone"]),
        body="org: sample-org\nrepos:\n  sample-app: auto\n  ghost: auto\n"
             "  sample-org/gone: manual\n")
    data = client.get("/api/config/repos/check").json()
    assert data["unreachable"] == ["ghost", "sample-org/gone"]
    assert data["repos"]["sample-app"]["status"] == "ok"
    assert data["repos"]["ghost"]["status"] == "missing"


def test_config_page_flags_and_can_remove_unreachable(tmp_path, monkeypatch):
    client, cfg_path = _cfg_client(
        tmp_path, monkeypatch, _gh_with_missing(["ghost"]),
        body="org: sample-org\nrepos:\n  sample-app: auto\n  ghost: auto\n")
    html = client.get("/config").text
    assert "1 repo cannot be reached" in html
    assert "ghost" in html

    assert client.delete("/api/config/repos/ghost").status_code == 200
    assert "ghost" not in load_config(cfg_path)["repos"]


def test_repo_check_results_are_cached_until_rechecked(tmp_path, monkeypatch):
    calls = []

    def gh(args, **kw):
        if args[1].startswith("orgs/"):
            return []
        calls.append(args[1])
        return args[1].removeprefix("repos/")

    client, _ = _cfg_client(tmp_path, monkeypatch, gh,
                            body="repos:\n  acme/thing: auto\n")
    client.get("/config")
    assert len(calls) == 1
    client.get("/config")            # cached — the page reloads after every edit
    assert len(calls) == 1
    client.get("/config?recheck=1")  # explicit re-check busts it
    assert len(calls) == 2


def test_repo_page_does_not_claim_auto_from_another_owners_bare_key(tmp_path, monkeypatch):
    """/repos/acme/api must not read `api: auto`, which means sample-org/api."""
    client, _ = _cfg_client(
        tmp_path, monkeypatch, lambda args, **kw: [],
        "org: sample-org\ndefault_mode: manual\nrepos:\n  api: auto\n")

    html = client.get("/repos/acme/api").text
    assert "MANUAL (default)" in html

    assert "AUTO" in client.get("/repos/sample-org/api").text


def test_toggling_mode_from_a_repo_page_targets_that_owner(tmp_path, monkeypatch):
    from src.autoreview_config import load_config as load_acfg

    client, cfg_path = _cfg_client(
        tmp_path, monkeypatch, lambda args, **kw: [], "org: sample-org\nrepos:\n  api: auto\n")

    r = client.post("/api/config/repos/acme%2Fapi/mode", json={"mode": "auto"})
    assert r.status_code == 200
    repos = load_acfg(cfg_path)["repos"]
    # The other owner's entry is untouched; a new, explicit one is created.
    assert repos == {"api": "auto", "acme/api": "auto"}


def test_config_routes_accept_owner_slash_repo_keys(tmp_path, monkeypatch):
    """Config keys contain slashes; a single-segment route 404'd on all of them."""
    from src.autoreview_config import load_config as load_acfg

    client, cfg_path = _cfg_client(
        tmp_path, monkeypatch, lambda args, **kw: [],
        "org: sample-org\nrepos:\n  acme/billing: auto\n  api: auto\n")

    r = client.post("/api/config/repos/acme%2Fbilling/mode",
                    json={"mode": "manual"})
    assert r.status_code == 200
    assert load_acfg(cfg_path)["repos"]["acme/billing"] == "manual"

    assert client.delete("/api/config/repos/acme%2Fbilling").status_code == 200
    assert "acme/billing" not in load_acfg(cfg_path)["repos"]

    # Bare keys keep working.
    assert client.delete("/api/config/repos/api").status_code == 200
    assert load_acfg(cfg_path)["repos"] == {}


def test_header_links_to_this_project_repo(tmp_path, monkeypatch):
    """The dashboard should say which tool it is, on every page."""
    client, _ = _cfg_client(tmp_path, monkeypatch, lambda args, **kw: [])

    for path in ("/", "/config"):
        html = client.get(path).text
        assert "nexpeakcore/deepseek-harness-pr-review" in html
        assert 'href="https://github.com/nexpeakcore/deepseek-harness-pr-review"' in html
        assert 'target="_blank"' in html
        # The GitHub mark is inlined, not fetched: the dashboard is a localhost
        # app and must render with no network.
        assert 'class="gh-mark"' in html
        assert "<svg" in html and "http" not in html.split('class="gh-mark"')[1][:200]


def test_project_repo_reads_metadata_not_a_hardcoded_url():
    """A fork must show its own repo, so the URL comes from the distribution."""
    from web.server import project_repo

    info = project_repo()
    assert info["url"].startswith("https://github.com/")
    assert info["name"] == info["url"].removeprefix("https://github.com/")


def test_header_omits_the_link_when_metadata_has_no_url(tmp_path, monkeypatch):
    """An install predating the Project-URL entry must still render."""
    import importlib.metadata

    from web.server import project_repo

    class NoUrls:
        def get_all(self, key):
            return None

    monkeypatch.setattr(importlib.metadata, "metadata", lambda name: NoUrls())
    assert project_repo() is None

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "metadata", missing)
    assert project_repo() is None


def test_gh_mark_carries_its_own_size(tmp_path, monkeypatch):
    """The mark must be 16px without CSS.

    An inline <svg> with no width/height falls back to the replaced-element
    default (300x150) the moment the stylesheet is missing or stale. A cached
    style.css did exactly that and turned the header into a full-page banner.
    """
    client, _ = _cfg_client(tmp_path, monkeypatch, lambda args, **kw: [])
    html = client.get("/").text
    assert '<svg class="gh-mark" width="16" height="16"' in html


def test_stylesheet_is_cache_busted(tmp_path, monkeypatch):
    """A CSS change must not need a hard refresh to take effect."""
    import src

    client, _ = _cfg_client(tmp_path, monkeypatch, lambda args, **kw: [])
    html = client.get("/").text
    assert f'href="/static/style.css?v={src.__version__}"' in html


def test_header_names_the_agent_backend(tmp_path, monkeypatch):
    """A review that quietly ran on the wrong provider must be visible up front.

    The dashboard hands its own environment to every review it starts, so the
    header is reporting the thing that actually does the work — not a second
    setting that could disagree with it.
    """
    monkeypatch.setenv("HARNESS_PROVIDER", "claude")
    monkeypatch.setenv("HARNESS_CLAUDE_MODEL", "opus")
    client, _ = _cfg_client(tmp_path, monkeypatch, lambda args, **kw: [])

    html = client.get("/").text
    assert "claude · <strong>opus</strong>" in html
    assert "HARNESS_PROVIDER=claude, HARNESS_CLAUDE_MODEL=opus" in html


def test_header_backend_follows_a_restart(tmp_path, monkeypatch):
    """Read per render, not cached at import — the value only matters when it changes."""
    monkeypatch.setenv("HARNESS_PROVIDER", "deepseek")
    monkeypatch.setenv("DSH_MODEL", "deepseek-v4-flash")
    client, _ = _cfg_client(tmp_path, monkeypatch, lambda args, **kw: [])
    assert "deepseek · <strong>deepseek-v4-flash</strong>" in client.get("/").text

    monkeypatch.setenv("HARNESS_PROVIDER", "claude")
    monkeypatch.setenv("HARNESS_CLAUDE_MODEL", "sonnet")
    assert "claude · <strong>sonnet</strong>" in client.get("/").text
