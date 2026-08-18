import json

import pytest

from src.human_gate import run_gate, trim_question


def test_trim_question_limits_words():
    q = "This is a very long question " * 10
    assert len(trim_question(q, max_words=20).split()) == 20


def test_trim_question_keeps_short():
    q = "Is the doc wrong?"
    assert trim_question(q) == q


def test_run_gate_writes_answers(tmp_path, monkeypatch):
    findings = {
        "claims": [{"id": "C1", "status": "UNVERIFIED", "evidence": [], "note": ""}],
        "docs": [{"path": "docs/a.md", "status": "WRONG",
                  "what": "doc says X, code does Y"}],
        "impact": [], "threads": [],
        "unresolved_questions": ["Is doc A correct, right?"],
    }
    monkeypatch.setattr("builtins.input",
                        lambda prompt: "y" if ("wrong" in prompt or "correct" in prompt) else "n")
    session_dir = tmp_path / "s"
    answers = run_gate(findings, session_dir)
    assert len(answers) == 3
    kinds = [a["kind"] for a in answers]
    assert kinds == ["doc", "claim", "free"]
    assert answers[0]["answer"] == "y"
    assert answers[1]["answer"] == "n"
    assert answers[2]["answer"] == "y"
    saved = json.loads((session_dir / "answers.json").read_text())
    assert len(saved) == 3
    assert all(set(a.keys()) == {"question", "kind", "answer"} for a in saved)


def test_run_gate_eof_no_stdin_marks_skipped(tmp_path, monkeypatch):
    """Không có stdin (web trigger / daemon / CI) → không crash, answer = SKIPPED."""
    findings = {
        "claims": [{"id": "C1", "status": "UNVERIFIED", "evidence": [], "note": ""}],
        "docs": [], "impact": [], "threads": [],
        "unresolved_questions": ["Any doubt?"],
    }

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    answers = run_gate(findings, tmp_path / "s")
    assert all(a["answer"] == "SKIPPED" for a in answers)
    assert len(answers) == 2
