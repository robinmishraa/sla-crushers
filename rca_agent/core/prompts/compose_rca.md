When you submit the RCA, two summaries must both be present — they serve different readers.

# `summary_technical` (for engineers)

- 2–4 sentences.
- Cite the *evidence that pinned it down* using tool call ids and concrete identifiers:
  > "Snowflake row count for `SCRAPING.PUBLIC.BLINKIT_SEARCH` dropped from ~12k/h → 0/h at 2025-04-12 03:00 UTC (E1, E2), exactly when PR #4123 merged at 02:55 UTC (E5) and changed the price selector in `scraping/spiders/blinkit_search.py:142` (E6)."
- Precise about scope: platform, module, country, identifiers, time window.
- No padding. No "monitor going forward". No "consider adding alerts".

# `summary_business` (for non-tech readers)

- **1–2 sentences. Plain English.**
- No table names, no SQL, no file paths, no PR numbers.
- State what happened from the user's perspective + what's expected next.

Examples:

> **Good (root_cause_found)**: "Our Blinkit search data stopped refreshing at 3am yesterday because a recent code change broke the price parser. A fix is in review — once merged, the data will start flowing again within an hour."

> **Good (needs_ui_verification)**: "We checked the warehouse and the data for Blinkit search is being collected and updated normally. Before we dig deeper, please confirm whether the platform dashboard is showing the latest data — the issue might be a display lag on the UI side."

> **Good (needs_deeper_probe)**: "We've ruled out the obvious causes (the spider is running fine and the database has recent rows). To pinpoint whether the website silently changed its response format, we've prepared a deeper test you can run with one click."

> **Bad**: "RCA finished. Multiple layers checked. Please review."

# `verdict_kind`

Pick deliberately — this is what the UI banner shows first.

- `root_cause_found` — green banner; the report leads with the top hypothesis + fix.
- `needs_ui_verification` — blue banner; the report leads with "data layers healthy — please check the dashboard". Include a `next_action` of kind `human_verify_ui` pointing at the specific dashboard / report the user should open.
- `needs_deeper_probe` — amber banner; the report lists the suggested deep probes as buttons.
- `inconclusive` — gray banner; only when even Tier 2 is exhausted.

# `next_actions`

Always include at least one. Format examples:

```json
{
  "kind": "human_verify_ui",
  "title": "Verify the Blinkit search dashboard",
  "detail": "Open the platform dashboard for Blinkit / search / India (2025-04-12) and confirm whether the rows shown match what's in the warehouse. If yes, the alert was a UI-cache issue; if no, ping the platform team.",
  "audience": "business"
}
```

```json
{
  "kind": "run_deep_probe",
  "title": "Pull yesterday's raw response for ASIN B08X",
  "detail": "Read one raw S3 artifact for the affected identifier so we can compare the upstream response shape against the current parser.",
  "audience": "technical",
  "suggested_tool": "s3.read",
  "suggested_args": { "key": "blinkit/search/2025-04-12/B08X.json" }
}
```

Set `audience` to:
- `business` for plain-English actions (verify on UI, escalate to the data team)
- `technical` for runnable probes / engineering follow-ups
- `both` if it's relevant to everyone (e.g. "RCA posted in Slack thread; thread will be updated when fix lands")

# Slack message rendering order

The Slack reply that gets posted in the thread will render in this order:

1. **Business summary** (always shown first)
2. Verdict banner with the chosen `verdict_kind`
3. Top hypothesis + confidence (if root_cause_found)
4. Chain-of-custody table
5. Next actions (filtered to `audience` ∈ {business, both} by default)
6. "View technical details" (collapsible) → technical summary + investigation checklist + ruled-out hypotheses
7. Proposed fix / draft PR link (if any)
