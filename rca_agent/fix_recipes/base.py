"""Recipe base: deterministic generators for the four allowed fix kinds.

The LLM proposes (kind, file_path, parameters); the recipe produces the actual
unified diff that gets applied. This avoids the LLM hallucinating syntactically
broken patches.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from rca_agent.core.settings import settings


@dataclass
class RecipeInput:
    file_path: str          # relative to scraping repo root
    rationale: str          # 1-line "why" comment to add
    params: dict            # recipe-specific


@dataclass
class RecipeOutput:
    file_path: str
    diff: str               # unified diff
    confidence: float


def _read(path: str) -> tuple[str, list[str]]:
    p = settings().SCRAPING_REPO_ROOT / path
    if not p.exists():
        raise FileNotFoundError(f"file not found in scraping repo: {path}")
    text = p.read_text(encoding="utf-8")
    return text, text.splitlines(keepends=False)


def make_unified_diff(file_path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
    )
