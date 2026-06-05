"""Thin wrapper around the Anthropic SDK.

Centralises:
  - Client construction.
  - Default model + max_tokens.
  - Loading of prompt files from `core/prompts/`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import anthropic

from rca_agent.core.settings import settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    if not settings().ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return anthropic.Anthropic(api_key=settings().ANTHROPIC_API_KEY)


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


def call_messages(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> Any:
    return get_client().messages.create(
        model=settings().LLM_MODEL,
        system=system,
        messages=messages,
        tools=tools or [],
        max_tokens=max_tokens,
        temperature=temperature,
    )
