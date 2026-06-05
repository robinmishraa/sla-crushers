"""Selector update recipe.

Takes a (file_path, old_selector, new_selector) tuple and produces a unified diff.
The recipe refuses to operate if the old selector occurs more than once in the file
(ambiguous), or zero times (already changed).
"""
from __future__ import annotations

from rca_agent.fix_recipes.base import RecipeInput, RecipeOutput, _read, make_unified_diff


def apply(rec: RecipeInput) -> RecipeOutput:
    old = rec.params["old_selector"]
    new = rec.params["new_selector"]
    text, _ = _read(rec.file_path)
    occurrences = text.count(old)
    if occurrences == 0:
        raise ValueError(f"selector {old!r} not found in {rec.file_path}")
    if occurrences > 1:
        raise ValueError(
            f"selector {old!r} occurs {occurrences} times in {rec.file_path}; refusing to apply"
        )
    after = text.replace(old, new, 1)
    diff = make_unified_diff(rec.file_path, text, after)
    return RecipeOutput(file_path=rec.file_path, diff=diff, confidence=0.85)
