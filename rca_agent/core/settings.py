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

from dotenv import load_dotenv

# sla-crushers/rca_agent/core/settings.py -> parents[2] = sla-crushers/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


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

    # LLM
    ANTHROPIC_API_KEY: str | None = _get("ANTHROPIC_API_KEY")
    LLM_MODEL: str = _get("RCA_LLM_MODEL", "claude-sonnet-4-5-20250929") or "claude-sonnet-4-5-20250929"

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

    # Persistence
    LOG_DIR: Path = Path(_get("RCA_LOG_DIR", str(PROJECT_ROOT / "runs" / "logs")))

    def __init__(self) -> None:
        self.SCRAPING_REPO_ROOT = _resolve_scraping_repo()
        if self.SCRAPING_REPO_ROOT is not None:
            # Make the scraping repo importable so we can reuse:
            #   common.config.database, temporal.resources.snowflake_client,
            #   temporal_v2.registry, etc.
            if str(self.SCRAPING_REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(self.SCRAPING_REPO_ROOT))
            # Also load the scraping repo's .env so DB_*, SF_*, AWS_*, SLACK_ANALYTICS_TOKEN
            # vars exist for tools that need them.
            load_dotenv(self.SCRAPING_REPO_ROOT / ".env")
            # Re-load our own .env on top so project-level overrides win.
            load_dotenv(PROJECT_ROOT / ".env", override=True)

        # Re-resolve any token whose value lives in the scraping repo's .env (loaded above).
        # Class-level field declarations evaluated before __init__ ran, so SLACK_ANALYTICS_TOKEN
        # wasn't in os.environ yet at that point.
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
