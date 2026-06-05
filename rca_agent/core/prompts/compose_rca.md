When you submit the RCA, the **summary** field is what reviewers read first. It must:

- Be 2–4 sentences.
- State the single most likely root cause in plain English.
- Quote the *evidence* that pinned it down (e.g., "Snowflake row count for blinkit_search dropped from 12k/h → 0/h at 2025-04-12 03:00 UTC, exactly when PR #4123 merged").
- Be precise about scope: which platform, module, country, identifiers, time window.

The Slack message will render in this order:
1. Summary
2. Top hypothesis + confidence
3. Chain-of-custody table
4. Investigation checklist (collapsible)
5. Ruled-out hypotheses (collapsible)
6. Proposed fix / draft PR link (if any)

Do not pad with generic advice ("monitor going forward", "consider adding alerts"). Reviewers want a verdict, not a postmortem.
