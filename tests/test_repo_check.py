import pytest

from src.repo_check import (FORBIDDEN, MISSING, OK, UNKNOWN, check_repo,
                            check_repos)


def _gh_raising(message):
    def gh(args, **kw):
        raise RuntimeError(message)
    return gh


def test_existing_repo_is_ok():
    result = check_repo("demo", "app", gh=lambda args, **kw: "demo/app")
    assert result["status"] == OK


def test_404_reads_as_missing():
    result = check_repo("demo", "gone",
                        gh=_gh_raising("gh api failed: gh: Not Found (HTTP 404)"))
    assert result["status"] == MISSING
    # 404 covers both "deleted" and "private to this token" — say so, don't guess.
    assert "not visible" in result["detail"]


@pytest.mark.parametrize("code", ["401", "403"])
def test_auth_failures_read_as_forbidden(code):
    result = check_repo("demo", "app",
                        gh=_gh_raising(f"gh api failed: gh: Forbidden (HTTP {code})"))
    assert result["status"] == FORBIDDEN
    assert code in result["detail"]


def test_network_failure_is_unknown_not_missing():
    """A blip must never be mistaken for a repo that does not exist."""
    result = check_repo("demo", "app",
                        gh=_gh_raising("dial tcp: connection refused"))
    assert result["status"] == UNKNOWN


def test_gh_binary_absent_is_unknown():
    def gh(args, **kw):
        raise OSError("No such file or directory: 'gh'")
    assert check_repo("demo", "app", gh=gh)["status"] == UNKNOWN


def test_empty_response_is_unknown():
    assert check_repo("demo", "app", gh=lambda args, **kw: "")["status"] == UNKNOWN


def test_check_repos_keys_by_full_name():
    def gh(args, **kw):
        if "gone" in args[1]:
            raise RuntimeError("gh: Not Found (HTTP 404)")
        return args[1].removeprefix("repos/")

    results = check_repos([("demo", "app"), ("demo", "gone")], gh=gh)
    assert results["demo/app"]["status"] == OK
    assert results["demo/gone"]["status"] == MISSING


def test_check_repos_empty_input():
    assert check_repos([]) == {}
