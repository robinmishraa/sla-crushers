"""Header / cookie / user-agent update recipe.

Takes (file_path, header_name, new_value). Replaces ONE assignment of that
header value. Refuses if the assignment can't be uniquely identified.
"""
from __future__ import annotations

import re

from rca_agent.fix_recipes.base import RecipeInput, RecipeOutput, _read, make_unified_diff


def apply(rec: RecipeInput) -> RecipeOutput:
    name = rec.params["header_name"]
    new_val = rec.params["new_value"]
    text, _ = _read(rec.file_path)

    # Match a key like "X-Header": "...",  or  'X-Header': '...'
    # Quote either by " or ', value can contain anything except the same quote.
    pattern = re.compile(
        r"(?P<lead>['\"])" + re.escape(name) + r"(?P=lead)\s*:\s*"
        r"(?P<q>['\"])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)",
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"header {name!r} not found in {rec.file_path}")
    if len(matches) > 1:
        raise ValueError(
            f"header {name!r} matched {len(matches)} times in {rec.file_path}; refusing to apply"
        )
    m = matches[0]
    quote = m.group("q")
    replacement = f"{m.group('lead')}{name}{m.group('lead')}: {quote}{new_val}{quote}"
    after = text[: m.start()] + replacement + text[m.end():]
    diff = make_unified_diff(rec.file_path, text, after)
    return RecipeOutput(file_path=rec.file_path, diff=diff, confidence=0.8)
