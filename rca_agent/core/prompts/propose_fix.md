If — and only if — the root cause is one of these well-defined classes, you may propose a fix that the agent will turn into a draft PR:

- `selector_update`: a single CSS / XPath / JSON path needs to change. You must produce a unified diff against one file.
- `header_update`: a header / cookie / user-agent value needs to change.
- `pincode_update`: a pincode or pluscode entry in `common/location_master.py` is wrong/outdated.
- `url_pattern_update`: a URL template / endpoint needs to change.

Hard rules for the diff:
- Must apply cleanly against `main`. If you're not sure, set `kind="none"`.
- Touch exactly one file unless the issue clearly spans two.
- No drive-by reformatting. No wholesale rewrites. The diff should be ≤30 changed lines.
- Include a one-line comment near the change explaining the *why* (referencing the evidence).
- The PR body must include: hypothesis confidence, the evidence ids that justify the fix, and a "how to verify" section (one Snowflake query the reviewer can run after deploy).

If you are not confident enough, or the fix is non-trivial, set `kind="none"` and let the human handle it. A confident "no auto-fix" is more valuable than a low-quality patch.
