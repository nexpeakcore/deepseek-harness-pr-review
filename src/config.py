"""Configuration from the environment. All env vars are optional.

A `.env` file next to the working directory is loaded first (simple KEY=VALUE
parser — no dotenv dependency); real environment variables win over it.
"""
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    # CWD .env (dev checkout / project dir) trước, rồi home .env cho bản cài
    # qua one-liner installer (chạy từ bất kỳ đâu). Biến môi trường thật luôn
    # thắng cả hai.
    candidates = [Path(".env"), Path.home() / ".harness-pr-review" / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# Agent backends. "deepseek" runs the Harness SDK against the DeepSeek API and
# needs DEEPSEEK_API_KEY; CLI providers carry their own credentials.
PROVIDERS = ("deepseek", "claude", "codex")
DEFAULT_PROVIDER = "deepseek"


@dataclass(frozen=True)
class Config:
    api_key: str
    model: str
    base_url: str
    session_root: Path
    provider: str = DEFAULT_PROVIDER
    claude_model: str = "sonnet"
    codex_model: str = "gpt-5.5"
    # The code axis costs one agent per ~20 changed files plus a verify agent,
    # roughly a third on top of a review. On by default; this is the off switch.
    code_review: bool = True

    @property
    def needs_deepseek_key(self) -> bool:
        """Only the DeepSeek backend is blocked by a missing DEEPSEEK_API_KEY.

        Asked as a property rather than compared inline, because three
        entry points gate on it — the CLI, the autoreview poller and the
        dashboard's Review now — and each drifting into its own
        `== "deepseek"` check is how one of them ends up demanding a key it
        never uses. The dashboard did exactly that and 400'd every review
        under the Claude backend while the CLI ran the same review fine.
        """
        return self.provider == "deepseek"

    def phase_cfg(self) -> dict:
        """Model config for phases 2 and 3, with `model` already resolved.

        Both phases take a plain dict, and both need the same answer to "which
        model is actually going to run". Resolving it once here keeps the
        provider fork out of the phase code, which cares about the model, not
        about where it came from.
        """
        return {
            "provider": self.provider,
            "model": (self.claude_model if self.provider == "claude" else
                      self.codex_model if self.provider == "codex" else self.model),
            "claude_model": self.claude_model,
            "codex_model": self.codex_model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "code_review": self.code_review,
        }


def load_config() -> Config:
    return Config(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        model=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        session_root=Path(os.environ.get("DSH_SESSION_ROOT", "sessions")),
        provider=os.environ.get("HARNESS_PROVIDER", DEFAULT_PROVIDER).strip().lower()
                 or DEFAULT_PROVIDER,
        claude_model=os.environ.get("HARNESS_CLAUDE_MODEL", "sonnet").strip() or "sonnet",
        codex_model=os.environ.get("HARNESS_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5",
        code_review=os.environ.get("HARNESS_CODE_REVIEW", "1").strip().lower()
                    not in ("0", "false", "no", "off"),
    )
