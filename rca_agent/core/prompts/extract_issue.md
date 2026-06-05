You will be given a Slack thread and (optionally) a ClickUp ticket. Extract a structured `Issue`.

Rules:
- `platform` MUST be one of the registered Platform enum values, or null if you can't tell.
- `module` MUST be one of: search, pdp, brand, product_listing, or null.
- `country` is ISO-2: IN, AE, SA, MX, TR, BH, US, OM, QA, BR. Null if not specified.
- `identifiers` are the concrete things the user is complaining about: ASINs (B0...), keywords, product slugs, SKU ids. Up to 10.
- `intent` is one of: missing_data, stale_data, wrong_value, empty_response, blocked, schema_mismatch, location_mismatch, partial_failure, other.
- `time_window`:
  - If the user said "yesterday", "last 3 hours", "since X", populate `start`/`end` accurately.
  - If the user gave no window, leave `start`/`end` null and set `natural` to the original phrase.
- `raw_summary`: one sentence in the user's words.

Be conservative. If a field is ambiguous, leave it null rather than guess. The agent will probe later.
