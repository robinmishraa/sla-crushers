"""Fix recipes registry.

The LLM proposes which recipe + parameters; we apply it deterministically.
"""
from __future__ import annotations

from rca_agent.core.schemas import FixProposalKind
from rca_agent.fix_recipes import header_update, pincode_update, selector_update, url_pattern_update
from rca_agent.fix_recipes.base import RecipeInput, RecipeOutput

RECIPES = {
    FixProposalKind.SELECTOR_UPDATE: selector_update.apply,
    FixProposalKind.HEADER_UPDATE: header_update.apply,
    FixProposalKind.PINCODE_UPDATE: pincode_update.apply,
    FixProposalKind.URL_PATTERN_UPDATE: url_pattern_update.apply,
}


def apply_recipe(kind: FixProposalKind, rec_input: RecipeInput) -> RecipeOutput:
    if kind not in RECIPES:
        raise ValueError(f"No recipe for kind={kind}")
    return RECIPES[kind](rec_input)
