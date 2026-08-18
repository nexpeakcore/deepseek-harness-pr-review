"""Headless PR review automation built on the DeepSeek Harness SDK."""
import importlib.metadata

DIST_NAME = "deepseek-harness-pr-review"

try:
    # Single source of truth is pyproject.toml, read through the installed
    # distribution. A hand-written literal here drifted to 1.0.6 while
    # pyproject said 1.1.0, and `--version` (which already read the metadata)
    # disagreed with `src.__version__` for several releases.
    __version__ = importlib.metadata.version(DIST_NAME)
except importlib.metadata.PackageNotFoundError:  # running from a plain checkout
    __version__ = "0+unknown"
