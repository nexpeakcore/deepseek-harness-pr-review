from src.docs_rank import changed_symbols, rank_docs


def _write(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_changed_symbols_across_languages():
    snapshot = {"files": [{"patch":
        "@@\n"
        "+def retry_payment(n):\n"
        "-class OldGateway:\n"
        "+export function chargeCard() {\n"
        "+const MAX_RETRIES = 5\n"
        "+  x = 1\n"
        "+def ab(self):\n"}]}
    symbols = changed_symbols(snapshot)
    assert {"retry_payment", "OldGateway", "chargeCard", "MAX_RETRIES"} <= symbols
    assert "ab" not in symbols       # too short to be a meaningful mention
    assert "x" not in symbols


def test_rank_docs_prefers_symbol_and_name_mentions(tmp_path):
    _write(tmp_path, "docs/payment.md", "The gateway calls retry_payment three times.")
    _write(tmp_path, "docs/unrelated.md", "How to brew coffee.")
    _write(tmp_path, "README.md", "A project.")
    snapshot = {"files": [{"filename": "src/payment.py",
                           "patch": "@@\n+def retry_payment(n):"}]}

    ranked = rank_docs(tmp_path, snapshot)
    paths = [r["path"] for r in ranked]
    assert paths[0] == "docs/payment.md"
    assert "docs/unrelated.md" not in paths
    assert "retry_payment" in ranked[0]["why"]


def test_rank_docs_boosts_docs_a_claim_named(tmp_path):
    _write(tmp_path, "docs/a.md", "nothing relevant")
    _write(tmp_path, "docs/b.md", "mentions payment.py a lot")
    snapshot = {"files": [{"filename": "src/payment.py", "patch": ""}]}
    ranked = rank_docs(tmp_path, snapshot,
                       claims=[{"id": "C1", "docs": ["docs/a.md"]}])
    assert ranked[0]["path"] == "docs/a.md"
    assert "named by a claim" in ranked[0]["why"]


def test_rank_docs_skips_docs_the_pr_itself_changed(tmp_path):
    _write(tmp_path, "docs/payment.md", "retry_payment")
    snapshot = {"files": [
        {"filename": "docs/payment.md", "patch": "@@\n+def retry_payment(n):"}]}
    # A doc edited by the PR is reviewed as a claim, not as a stale doc.
    assert [r["path"] for r in rank_docs(tmp_path, snapshot)] == []


def test_rank_docs_ignores_generated_trees_and_non_prose(tmp_path):
    _write(tmp_path, "node_modules/pkg/README.md", "payment.py")
    _write(tmp_path, "src/thing.egg-info/SOURCES.md", "payment.py")
    _write(tmp_path, "requirements.txt", "payment.py")
    _write(tmp_path, "docs/real.md", "payment.py")
    snapshot = {"files": [{"filename": "src/payment.py", "patch": ""}]}
    assert [r["path"] for r in rank_docs(tmp_path, snapshot)] == ["docs/real.md"]


def test_rank_docs_respects_top_n(tmp_path):
    for i in range(30):
        _write(tmp_path, f"docs/d{i}.md", "payment.py")
    snapshot = {"files": [{"filename": "src/payment.py", "patch": ""}]}
    assert len(rank_docs(tmp_path, snapshot, top_n=5)) == 5
