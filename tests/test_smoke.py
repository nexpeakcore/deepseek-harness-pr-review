def test_package_imports():
    import src  # noqa: F401

    assert src.__version__


def test_version_matches_pyproject():
    """One version, one source. These drifted (1.0.6 vs 1.1.0) once already."""
    import re
    import tomllib
    from pathlib import Path

    import src

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert src.__version__ == declared, (
        f"src.__version__ ({src.__version__}) != pyproject ({declared}) — "
        f"reinstall the package (pip install -e .) or fix the declaration")
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared)
