"""Centralised settings, loaded from .env.

This project (`sla-crushers`) lives in its own repo. To do its work the agent
must read code, configs, and tooling from a local clone of the scraping repo.
That clone's path is configured via the `SCRAPING_REPO_ROOT` env var.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# sla-crushers/rca_agent/core/settings.py -> parents[2] = sla-crushers/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _apply_dotenv_skipping_placeholders(path: Path, override: bool = False) -> None:
    """Like load_dotenv() but never lets empty / '...'-suffix placeholder values
    into os.environ — those would shadow real values loaded later (e.g. from
    the scraping repo's .env). Idempotent."""
    if not path.exists():
        return
    for k, v in (dotenv_values(path) or {}).items():
        if v is None:
            continue
        v = v.strip()
        if not v or v.endswith("..."):
            continue
        if override or k not in os.environ:
            os.environ[k] = v


# Module-import load: only the real values from sla-crushers/.env. Class-level
# field declarations below need these in os.environ. The scraping repo's .env
# is then loaded in Settings.__init__ to fill in DB_*, SF_*, AWS_*, etc.
_apply_dotenv_skipping_placeholders(PROJECT_ROOT / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name, default)
    if val in (None, ""):
        return None
    # Treat .env.example placeholders (e.g. "xoxb-...", "sk-ant-...", "pk_...", "ghp_...")
    # as unset so fallbacks (like SLACK_ANALYTICS_TOKEN) kick in.
    if isinstance(val, str) and val.endswith("..."):
        return None
    return val


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_scraping_repo() -> Path | None:
    """Find the local scraping repo clone.

    Returns None (rather than raising) when the path is missing or invalid.
    Tools that genuinely need the scraping repo will report the situation at
    call time. This lets the FastAPI app boot and the UI render even if .env
    is half-configured — useful for hackathon demos.
    """
    raw = _get("SCRAPING_REPO_ROOT")
    if raw:
        p = Path(raw).expanduser().resolve()
        if p.exists():
            return p
        return None
    # Heuristic default: sibling directory named 'scraping'
    candidate = (PROJECT_ROOT.parent / "scraping").resolve()
    if candidate.exists() and (candidate / "common" / "config.py").exists():
        return candidate
    return None


class Settings:
    PROJECT_ROOT: Path = PROJECT_ROOT
    SCRAPING_REPO_ROOT: Path | None = None
    SCRAPING_REPO_ROOT_RAW: str | None = _get("SCRAPING_REPO_ROOT")

    # LLM — we auto-detect the provider from the key prefix:
    #   sk-or-...  → OpenRouter (OpenAI-compatible API). Set during the hackathon.
    #   sk-ant-... → Anthropic direct.
    # Models are namespaced differently on each, so we pick a sensible default per provider.
    ANTHROPIC_API_KEY: str | None = _get("ANTHROPIC_API_KEY")
    _user_model: str | None = _get("RCA_LLM_MODEL")

    @property
    def LLM_PROVIDER(self) -> str:
        key = self.ANTHROPIC_API_KEY or ""
        if key.startswith("sk-or-"):
            return "openrouter"
        return "anthropic"

    @property
    def LLM_MODEL(self) -> str:
        provider = self.LLM_PROVIDER
        user = self._user_model

        if provider == "openrouter":
            # If the user explicitly provided an OpenRouter-style namespaced model, honor it.
            if user and "/" in user:
                return user
            # Translate common Anthropic model IDs to their OpenRouter equivalents
            # so the same RCA_LLM_MODEL value works across providers.
            mapping = {
                "claude-sonnet-4-5-20250929":   "anthropic/claude-sonnet-4.5",
                "claude-3-7-sonnet-20250219":   "anthropic/claude-3.7-sonnet",
                "claude-3-5-sonnet-20241022":   "anthropic/claude-3.5-sonnet",
                "claude-opus-4-20250514":       "anthropic/claude-opus-4",
            }
            if user and user in mapping:
                return mapping[user]
            # Sensible default for hackathon
            return "anthropic/claude-sonnet-4.5"

        # Anthropic direct
        return user or "claude-sonnet-4-5-20250929"

    # Slack — accept SLACK_ANALYTICS_TOKEN as a fallback for SLACK_BOT_TOKEN
    # (the scraping repo's existing .env uses that name for the same xoxb token).
    SLACK_BOT_TOKEN: str | None = _get("SLACK_BOT_TOKEN") or _get("SLACK_ANALYTICS_TOKEN")
    SLACK_APP_TOKEN: str | None = _get("SLACK_APP_TOKEN")
    SLACK_SIGNING_SECRET: str | None = _get("SLACK_SIGNING_SECRET")

    # ClickUp
    CLICKUP_API_TOKEN: str | None = _get("CLICKUP_API_TOKEN")

    # GitHub
    GITHUB_TOKEN: str | None = _get("GITHUB_TOKEN")
    GITHUB_REPO: str = _get("GITHUB_REPO", "GobbleCube/scraping") or "GobbleCube/scraping"
    GITHUB_BASE_BRANCH: str = _get("GITHUB_DEFAULT_BASE_BRANCH", "main") or "main"

    # Runtime budgets
    TOOL_BUDGET: int = _get_int("RCA_TOOL_BUDGET", 25)
    SF_STATEMENT_TIMEOUT_S: int = _get_int("RCA_SF_STATEMENT_TIMEOUT_S", 60)
    SF_DEFAULT_LIMIT: int = _get_int("RCA_SF_DEFAULT_LIMIT", 200)
    PG_DEFAULT_LIMIT: int = _get_int("RCA_PG_DEFAULT_LIMIT", 200)
    AUTO_PR_MIN_CONFIDENCE: float = _get_float("RCA_AUTO_PR_MIN_CONFIDENCE", 0.75)

    # If true, the orchestrator will NOT post the RCA back into the Slack thread —
    # the RCA still renders in the UI. Useful when the bot lacks chat:write or
    # when you just want a dry run. Default true for hackathon safety.
    SKIP_SLACK_POST: bool = (_get("RCA_SKIP_SLACK_POST", "true") or "true").lower() in ("1", "true", "yes")

    # Persistence
    LOG_DIR: Path = Path(_get("RCA_LOG_DIR", str(PROJECT_ROOT / "runs" / "logs")))

    def __init__(self) -> None:
        self.SCRAPING_REPO_ROOT = _resolve_scraping_repo()
        if self.SCRAPING_REPO_ROOT is not None:
            # Make the scraping repo importable (common.config.database,
            # temporal.resources.snowflake_client, temporal_v2.registry, etc.)
            if str(self.SCRAPING_REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(self.SCRAPING_REPO_ROOT))
            # Pull DB_*, SF_*, AWS_*, SLACK_ANALYTICS_TOKEN from the scraping repo's .env.
            load_dotenv(self.SCRAPING_REPO_ROOT / ".env")
            # Apply our .env on top, but ONLY for values that are genuinely set —
            # empty placeholders like "SF_RSA_KEY=" or "GITHUB_TOKEN=ghp_..." must NOT
            # clobber real values from the scraping repo's .env.
            for k, v in (dotenv_values(PROJECT_ROOT / ".env") or {}).items():
                if v is None:
                    continue
                v = v.strip()
                if not v or v.endswith("..."):
                    continue
                os.environ[k] = v

        # Re-resolve tokens that came from the scraping repo's .env (loaded above)
        # — class-level field declarations evaluated before __init__ ran.
        slack_resolved = _get("SLACK_BOT_TOKEN") or _get("SLACK_ANALYTICS_TOKEN")
        if slack_resolved:
            self.SLACK_BOT_TOKEN = slack_resolved

    def require_scraping_repo(self) -> Path:
        """Raise a clean RuntimeError if the scraping repo is not configured."""
        if self.SCRAPING_REPO_ROOT is None:
            raw = self.SCRAPING_REPO_ROOT_RAW
            if raw:
                msg = (
                    f"SCRAPING_REPO_ROOT={raw!r} does not exist on this machine. "
                    "Edit .env in the project root and point it at your local clone of "
                    "GobbleCube/scraping (e.g. ~/Desktop/scraping)."
                )
            else:
                msg = (
                    "SCRAPING_REPO_ROOT is not set. Edit .env in the project root and "
                    "add an absolute path to your local clone of GobbleCube/scraping."
                )
            raise RuntimeError(msg)
        return self.SCRAPING_REPO_ROOT


@lru_cache(maxsize=1)
def settings() -> Settings:
    s = Settings()
    s.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return s
