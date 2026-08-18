import json

import pytest

from src.claims import extract_claims


FIXTURE_RESPONSE = """```json
[
  {"id": "C1", "text": "Adds checkout", "category": "feature",
   "files": ["src/checkout.py"], "docs": []},
  {"id": "C2", "text": "Fixes payment retry", "category": "bugfix",
   "files": ["src/payment.py"], "docs": ["docs/payment.md"]}
]
```"""


def test_extract_claims(tmp_path):
    snapshot = {
        "title": "Add checkout flow",
        "body": "Adds checkout. Fixes payment retry.",
        "files": [{"filename": "src/checkout.py"}, {"filename": "src/payment.py"}],
    }
    session_dir = tmp_path / "s"
    claims = extract_claims(
        snapshot,
        {"model": "m", "api_key": "k", "base_url": "http://x/v1"},
        session_dir,
        chat=lambda messages, **kw: FIXTURE_RESPONSE,
    )
    assert claims[0]["id"] == "C1"
    assert claims[1]["category"] == "bugfix"
    assert claims[1]["docs"] == ["docs/payment.md"]
    saved = json.loads((session_dir / "claims.json").read_text())
    assert len(saved) == 2


def test_extract_claims_invalid_response(tmp_path):
    session_dir = tmp_path / "s"
    with pytest.raises(RuntimeError, match="invalid claims response"):
        extract_claims(
            {"title": "t", "body": "b", "files": []},
            {"model": "m", "api_key": "k", "base_url": "http://x/v1"},
            session_dir,
            chat=lambda messages, **kw: "not json at all",
        )


INFERRED_RESPONSE = """[
  {"id": "C1", "text": "Retries failed payments up to 5 times", "category": "bugfix",
   "files": ["src/payment.py"], "docs": []}
]"""

THIN_SNAPSHOT = {
    "title": "fix",
    "body": "",
    "head": "fix/payment-retry",
    "base": "main",
    "labels": ["bug"],
    "commits": [{"sha": "a1", "message": "fix: retry payments 5 times\n\ndetail"}],
    "files": [
        {"filename": "src/payment.py", "status": "modified", "additions": 4,
         "deletions": 2, "patch": "@@\n-RETRIES = 3\n+RETRIES = 5"},
        {"filename": "tests/test_payment.py", "status": "modified", "additions": 3,
         "deletions": 0, "patch": "@@\n+assert RETRIES == 5"},
    ],
    "linked_issues": [{"number": 12, "title": "Payments fail once", "body": "One retry is not enough."}],
    "threads": [],
}


def _cfg():
    return {"model": "m", "api_key": "k", "base_url": "http://x/v1"}


def test_description_is_thin():
    from src.claims import description_is_thin

    assert description_is_thin({"body": ""})
    assert description_is_thin({"body": "fix"})
    assert description_is_thin({"body": "Closes #123"})
    assert description_is_thin({"body": "<!-- describe your change here -->"})
    assert description_is_thin({"body": "## Summary\n- [ ] tests\n- [ ] docs"})
    assert not description_is_thin(
        {"body": "Adds checkout. Fixes payment retry."})


def test_empty_description_falls_back_to_inferred(tmp_path):
    seen = []

    def chat(messages, **kw):
        seen.append(messages[0]["content"])
        return INFERRED_RESPONSE

    claims = extract_claims(THIN_SNAPSHOT, _cfg(), tmp_path / "s", chat=chat)
    # Only the inferred pass runs — no point asking an LLM to split an empty body.
    assert len(seen) == 1
    assert "no usable description" in seen[0]
    assert claims[0]["source"] == "inferred"
    saved = json.loads((tmp_path / "s" / "claims.json").read_text())
    assert saved[0]["source"] == "inferred"


def test_stated_pass_returning_nothing_falls_back(tmp_path):
    snapshot = {**THIN_SNAPSHOT, "body": "This pull request updates some things now."}
    responses = ["[]", INFERRED_RESPONSE]
    claims = extract_claims(snapshot, _cfg(), tmp_path / "s",
                            chat=lambda messages, **kw: responses.pop(0))
    assert not responses
    assert claims[0]["source"] == "inferred"


def test_stated_claims_are_tagged(tmp_path):
    claims = extract_claims(
        {"title": "t", "body": "Adds checkout. Fixes payment retry.", "files": []},
        _cfg(), tmp_path / "s", chat=lambda messages, **kw: FIXTURE_RESPONSE)
    assert [c["source"] for c in claims] == ["stated", "stated"]


def test_intent_signals_gathers_non_description_evidence():
    from src.claims import intent_signals

    text = intent_signals(THIN_SNAPSHOT)
    assert "fix/payment-retry" in text
    assert "fix: retry payments 5 times" in text
    assert "Linked issue #12" in text
    assert "One retry is not enough." in text
    assert "bug" in text


def test_diff_digest_puts_tests_first_and_bounds_size():
    from src.claims import diff_digest

    digest = diff_digest(THIN_SNAPSHOT)
    assert digest.index("tests/test_payment.py") < digest.index("src/payment.py")

    big = {"files": [{"filename": f"f{i}.py", "status": "modified", "additions": 1,
                      "deletions": 0, "patch": "x" * 5000} for i in range(60)]}
    assert len(diff_digest(big)) < 70_000


def test_all_inferred():
    from src.claims import all_inferred

    assert all_inferred([{"source": "inferred"}])
    assert not all_inferred([{"source": "inferred"}, {"source": "stated"}])
    assert not all_inferred([])
    assert not all_inferred(None)
