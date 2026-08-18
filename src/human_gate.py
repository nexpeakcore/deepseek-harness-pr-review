"""Phase 4: human-in-the-loop — ask for confirmation when docs are wrong or claims are uncertain."""
import json
from pathlib import Path


def trim_question(q: str, max_words: int = 20) -> str:
    words = q.split()
    if len(words) <= max_words:
        return q
    return " ".join(words[:max_words])


def _collect_questions(findings: dict) -> list[tuple[str, str]]:
    """Return [(question, kind)] — kind is used to decide how to ask."""
    questions: list[tuple[str, str]] = []
    for d in findings.get("docs", []):
        if d["status"] in ("WRONG", "FABRICATED"):
            q = trim_question(
                f"Doc {d.get('path', '?')}: {d.get('what', '')}. Is the doc wrong?")
            questions.append((q, "doc"))
    for c in findings.get("claims", []):
        if c["status"] == "UNVERIFIED":
            q = trim_question(f"Claim {c['id']} could not be verified. Keep UNVERIFIED?")
            questions.append((q, "claim"))
    for q in findings.get("unresolved_questions", []):
        questions.append((trim_question(q), "free"))
    return questions


def run_gate(findings: dict, session_dir: Path, interactive: bool = True) -> list[dict]:
    """Ask each question (≤20 words). Save answers.json. Return the list of answers."""
    answers: list[dict] = []
    for question, kind in _collect_questions(findings):
        if interactive:
            try:
                answer = input(f"[harness] {question} (y/n or free text): ").strip()
            except EOFError:
                # Không có stdin (web-triggered review, CI, daemon) — không crash,
                # đánh dấu SKIPPED để pipeline tiếp tục.
                answer = "SKIPPED"
        else:
            answer = "SKIPPED"
        answers.append({"question": question, "kind": kind, "answer": answer})

    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "answers.json").write_text(json.dumps(answers, indent=2))
    return answers
