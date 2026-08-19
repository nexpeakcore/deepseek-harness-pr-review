import json

import pytest

from src.gh import gh_available, run_gh


def test_run_gh_json(monkeypatch):
    captured = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        assert text is True
        return type("R", (), {"returncode": 0, "stdout": json.dumps({"ok": 1}), "stderr": ""})()

    monkeypatch.setattr("src.gh.subprocess.run", fake_run)
    out = run_gh(["api", "repos/x/y/pulls/1"])
    assert out == {"ok": 1}
    assert "gh" in captured["cmd"][0]


def test_run_gh_error(monkeypatch):
    def fake_run(cmd, capture_output, text):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()

    monkeypatch.setattr("src.gh.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="gh api failed: not found"):
        run_gh(["api", "repos/x/y/pulls/1"])


def test_run_gh_invalid_json(monkeypatch):
    def fake_run(cmd, capture_output, text):
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("src.gh.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_gh(["api", "repos/x/y"])


def test_gh_available(monkeypatch):
    def fake_run(cmd, capture_output, text):
        return type("R", (), {"returncode": 0, "stdout": "1\n", "stderr": ""})()

    monkeypatch.setattr("src.gh.subprocess.run", fake_run)
    assert gh_available() is True


def _gh_returning(stdout):
    def fake_run(cmd, capture_output, text):
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    return fake_run


def test_paginated_pages_are_concatenated(monkeypatch):
    """`gh api --paginate` emits one JSON document PER PAGE, not one overall.

    A PR that grows past 100 changed files turns the response into
    `[...]\n[...]` — two valid documents, invalid JSON together. This crashed
    phase 1 with "Extra data: line 2 column 1" on a 120-file PR.
    """
    page1 = json.dumps([{"filename": f"f{i}.py"} for i in range(100)])
    page2 = json.dumps([{"filename": f"f{i}.py"} for i in range(100, 120)])
    monkeypatch.setattr("src.gh.subprocess.run", _gh_returning(f"{page1}\n{page2}"))

    out = run_gh(["api", "repos/x/y/pulls/1/files", "--paginate"])
    assert len(out) == 120
    assert out[0]["filename"] == "f0.py"
    assert out[-1]["filename"] == "f119.py"


def test_three_pages(monkeypatch):
    stdout = "\n".join(json.dumps([i]) for i in range(3))
    monkeypatch.setattr("src.gh.subprocess.run", _gh_returning(stdout))
    assert run_gh(["api", "x", "--paginate"]) == [0, 1, 2]


def test_single_page_is_unchanged(monkeypatch):
    """The common case must not be reshaped by the multi-page handling."""
    monkeypatch.setattr("src.gh.subprocess.run",
                        _gh_returning(json.dumps([{"a": 1}, {"a": 2}])))
    assert run_gh(["api", "x", "--paginate"]) == [{"a": 1}, {"a": 2}]

    monkeypatch.setattr("src.gh.subprocess.run", _gh_returning(json.dumps({"a": 1})))
    assert run_gh(["api", "x"]) == {"a": 1}


def test_non_list_documents_are_not_flattened(monkeypatch):
    """Only pages of a collection concatenate; separate objects stay separate."""
    monkeypatch.setattr("src.gh.subprocess.run",
                        _gh_returning('{"a": 1}\n{"b": 2}'))
    assert run_gh(["api", "x", "--paginate"]) == [{"a": 1}, {"b": 2}]


def test_whitespace_and_blank_lines_between_pages(monkeypatch):
    monkeypatch.setattr("src.gh.subprocess.run",
                        _gh_returning('  [1]  \n\n\n  [2]\n  '))
    assert run_gh(["api", "x", "--paginate"]) == [1, 2]


def test_truly_malformed_output_still_raises(monkeypatch):
    monkeypatch.setattr("src.gh.subprocess.run", _gh_returning("[1] garbage"))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_gh(["api", "x"])
