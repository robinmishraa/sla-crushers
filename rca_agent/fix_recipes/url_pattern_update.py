"""URL pattern update recipe.

Takes (file_path, old_pattern, new_pattern). Treats old_pattern as a literal
string (not regex). Single-occurrence rule applies.
"""
from __future__ import annotations

from rca_agent.fix_recipes.base import RecipeInput, RecipeOutput, _read, make_unified_diff


def apply(rec: RecipeInput) -> RecipeOutput:
    old = rec.params["old_pattern"]
    new = rec.params["new_pattern"]
    text, _ = _read(rec.file_path)
    if text.count(old) != 1:
        raise ValueError(
            f"URL pattern {old!r} occurs {text.count(old)} times in {rec.file_path}; refusing to apply"
        )
    after = text.replace(old, new, 1)
    diff = make_unified_diff(rec.file_path, text, after)
    return RecipeOutput(file_path=rec.file_path, diff=diff, confidence=0.85)
