# tests/test_server.py
import json
import os

import pytest
from fastapi.testclient import TestClient

from src.autoreview_config import load_config
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

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr("web.server.run_main", fake_main)
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    args = calls[0]
    assert "sample-org/sample-app" in args and "78" in args
    assert "--force" in args
    assert "--skip-human" in args       # config mặc định skip_human: true
    assert "--no-post" not in args      # config mặc định post_comment: true


def test_trigger_review_no_post_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_SESSION_ROOT", str(tmp_path / "sessions"))
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n"
                        "post_comment: false\nskip_human: false\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr("web.server.run_main", fake_main)
    client = TestClient(app)
    r = client.post("/api/repos/sample-org/sample-app/pr/78/review")
    assert r.status_code == 200
    assert "--no-post" in calls[0]
    assert "--skip-human" not in calls[0]


def test_trigger_review_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg_path = tmp_path / "autoreview.yml"
    cfg_path.write_text("org: sample-org\nrepos:\n  sample-app: auto\n")
    monkeypatch.setenv("AUTOREVIEW_CONFIG", str(cfg_path))
    monkeypatch.setattr("web.server.run_main", lambda argv: 3)  # thiếu API key
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
    monkeypatch.setattr("web.server.run_main", lambda argv: 2)  # gh lỗi
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
    monkeypatch.setattr("web.server.run_main", lambda argv: 0)
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
