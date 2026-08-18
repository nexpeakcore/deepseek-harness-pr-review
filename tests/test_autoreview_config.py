# tests/test_autoreview_config.py
import pytest

from src.autoreview_config import (auto_repos, list_repos, load_config,
                               remove_repo, set_repo_mode)

NEW_YML = """
org: sample-org
default_mode: manual
interval_minutes: 2
post_comment: true
skip_human: true
drafts: false
repos:
  sample-app: auto
  sample-api: auto
"""


def _write(path, text):
    path.write_text(text)
    return path


def test_load_config_new_format(tmp_path):
    cfg = load_config(_write(tmp_path / "a.yml", NEW_YML))
    assert cfg["org"] == "sample-org"
    assert cfg["default_mode"] == "manual"
    assert cfg["repos"] == {"sample-app": "auto", "sample-api": "auto"}
    assert cfg["interval_minutes"] == 2


def test_load_config_legacy_list(tmp_path):
    cfg = load_config(_write(tmp_path / "a.yml",
                             "repos:\n  - sample-org/sample-app\n"))
    assert cfg["repos"] == {"sample-org/sample-app": "auto"}


def test_load_config_empty_repos_allowed(tmp_path):
    cfg = load_config(_write(tmp_path / "a.yml", "org: sample-org\n"))
    assert cfg["repos"] == {}
    assert cfg["default_mode"] == "manual"


def test_load_config_invalid_mode(tmp_path):
    with pytest.raises(ValueError, match="mode must be auto|manual"):
        load_config(_write(tmp_path / "a.yml",
                           "repos:\n  sample-app: sometimes\n"))


def test_load_config_invalid_interval(tmp_path):
    with pytest.raises(ValueError, match="interval_minutes"):
        load_config(_write(tmp_path / "a.yml", "interval_minutes: -5\n"))


def test_load_config_bad_yaml(tmp_path):
    with pytest.raises(ValueError, match="invalid config YAML"):
        load_config(_write(tmp_path / "a.yml", "repos: [unclosed\n"))


def test_set_repo_mode_add_and_change(tmp_path):
    p = _write(tmp_path / "a.yml", "org: sample-org\nrepos:\n  sample-app: manual\n")
    set_repo_mode(p, "admin-web", "auto")          # add by name
    cfg = load_config(p)
    assert cfg["repos"]["admin-web"] == "auto"
    set_repo_mode(p, "sample-app", "auto")          # change
    assert load_config(p)["repos"]["sample-app"] == "auto"


def test_set_repo_mode_invalid(tmp_path):
    p = _write(tmp_path / "a.yml", "org: sample-org\n")
    with pytest.raises(ValueError, match="mode must be auto|manual"):
        set_repo_mode(p, "sample-app", "banana")


def test_remove_repo(tmp_path):
    p = _write(tmp_path / "a.yml", NEW_YML)
    remove_repo(p, "sample-api")
    cfg = load_config(p)
    assert "sample-api" not in cfg["repos"]
    assert "sample-app" in cfg["repos"]


def test_auto_repos_resolves_org(tmp_path):
    cfg = load_config(_write(tmp_path / "a.yml", NEW_YML))
    assert auto_repos(cfg) == [("sample-org", "sample-app"),
                               ("sample-org", "sample-api")]


def test_auto_repos_full_path(tmp_path):
    cfg = load_config(_write(
        tmp_path / "a.yml",
        "repos:\n  other/legacy: auto\n  sample-app: manual\n"))
    assert auto_repos(cfg) == [("other", "legacy")]


def test_list_repos_with_org_merge(tmp_path):
    p = _write(tmp_path / "a.yml", NEW_YML)  # sample-app auto, sample-api auto

    def fake_gh(args, **kw):
        assert "orgs/sample-org/repos" in args[1]
        return [{"name": "sample-app"}, {"name": "admin-web"}]

    rows = list_repos(p, gh=fake_gh)
    by_name = {r["name"]: r["mode"] for r in rows}
    assert by_name["sample-app"] == "auto"
    assert by_name["admin-web"] == "unlisted"
    assert by_name["sample-api"] == "auto"   # configured but not in org list


def test_list_repos_without_org(tmp_path):
    p = _write(tmp_path / "a.yml", "repos:\n  sample-app: auto\n")
    rows = list_repos(p, gh=lambda args, **kw: None)  # gh không được gọi
    assert rows == [{"name": "sample-app", "mode": "auto"}]


def test_max_parallel_defaults_to_sequential(tmp_path):
    path = tmp_path / "autoreview.yml"
    path.write_text("org: o\nrepos:\n  a: auto\n")
    cfg = load_config(path)
    assert cfg["max_parallel"] == 1
    assert cfg["review_timeout_minutes"] == 30


@pytest.mark.parametrize("value", ["0", "-1", '"3"', "9", "true", "1.5"])
def test_max_parallel_rejects_bad_values(tmp_path, value):
    """0/âm/quá cap/chuỗi/bool/float đều bị từ chối ngay khi load config."""
    path = tmp_path / "autoreview.yml"
    path.write_text(f"org: o\nrepos:\n  a: auto\nmax_parallel: {value}\n")
    with pytest.raises(ValueError, match="max_parallel"):
        load_config(path)


def test_review_timeout_rejects_bad_values(tmp_path):
    path = tmp_path / "autoreview.yml"
    path.write_text("org: o\nrepos:\n  a: auto\nreview_timeout_minutes: 0\n")
    with pytest.raises(ValueError, match="review_timeout_minutes"):
        load_config(path)


def test_ping_comment_defaults_on(tmp_path):
    """Ping bật mặc định: không có nó thì re-review hoàn toàn im lặng."""
    path = tmp_path / "autoreview.yml"
    path.write_text("org: o\nrepos:\n  a: auto\n")
    assert load_config(path)["ping_comment"] is True


def test_max_agents_defaults_and_validates(tmp_path):
    from src import agent_pool
    from src.autoreview_config import DEFAULTS, load_config, validate_config

    path = tmp_path / "autoreview.yml"
    path.write_text("repos: {}\n")
    assert load_config(path)["max_agents"] == agent_pool.DEFAULT_MAX_AGENTS

    for bad in (0, -1, agent_pool.MAX_AGENTS_CAP + 1, "4", True, None):
        with pytest.raises(ValueError, match="max_agents"):
            validate_config({**DEFAULTS, "repos": {}, "max_agents": bad})


def test_max_agents_travels_to_the_review_subprocess(tmp_path, monkeypatch):
    from src.review_proc import run_review

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr("review_proc.subprocess.run", fake_run)
    run_review("o", "r", 1, session_root=tmp_path,
               log_path=tmp_path / "l.log", max_agents=6)
    assert seen["HARNESS_MAX_AGENTS"] == "6"


def test_bare_key_belongs_to_the_configured_org_only():
    """`api: auto` means <org>/api — not every owner's repo called api.

    The dashboard used to fall back to a bare-name lookup with no owner check,
    so /repos/acme/api showed AUTO on the strength of an entry that
    auto_repos() resolves to sample-org/api — a repo the poller never touches.
    """
    from src.autoreview_config import repo_mode

    cfg = {"org": "sample-org", "repos": {"api": "auto",
                                          "acme/billing": "manual"}}
    assert repo_mode(cfg, "sample-org", "api") == "auto"
    assert repo_mode(cfg, "acme", "api") is None
    assert repo_mode(cfg, "acme", "billing") == "manual"
    assert repo_mode(cfg, "other", "nothing") is None


def test_bare_key_matches_nothing_when_no_org_is_set():
    from src.autoreview_config import repo_mode

    cfg = {"org": "", "repos": {"api": "auto"}}
    assert repo_mode(cfg, "anyone", "api") is None


def test_repo_mode_agrees_with_auto_repos():
    """The badge and the poller must not disagree about what is auto."""
    from src.autoreview_config import auto_repos, repo_mode

    cfg = {"org": "sample-org",
           "repos": {"api": "auto", "acme/billing": "auto", "x": "manual"}}
    for owner, repo in auto_repos(cfg):
        assert repo_mode(cfg, owner, repo) == "auto"
    assert repo_mode(cfg, "acme", "api") is None
    assert ("acme", "api") not in auto_repos(cfg)


def test_set_mode_updates_the_bare_key_instead_of_duplicating(tmp_path):
    from src.autoreview_config import load_config, set_repo_mode

    path = tmp_path / "autoreview.yml"
    path.write_text("org: sample-org\nrepos:\n  api: auto\n")
    set_repo_mode(path, "sample-org/api", "manual")
    repos = load_config(path)["repos"]
    assert repos == {"api": "manual"}          # no second entry for the same repo


def test_set_mode_for_another_owner_creates_its_own_key(tmp_path):
    from src.autoreview_config import load_config, set_repo_mode

    path = tmp_path / "autoreview.yml"
    path.write_text("org: sample-org\nrepos:\n  api: auto\n")
    set_repo_mode(path, "acme/api", "auto")
    repos = load_config(path)["repos"]
    assert repos == {"api": "auto", "acme/api": "auto"}
