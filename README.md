# sla-crushers

> AI agent that turns a Slack `#dogfooding` link into a defensible RCA — Snowflake, Postgres, raw S3 artifacts, spider source, git history, and PRs all checked. Optionally opens a draft fix PR.

The repetitive part of debugging a scraping issue is mechanical: parse the Slack message → read the ClickUp ticket → open Snowflake and run 4–6 queries → cross-check Postgres → grep the spider source → check git log for recent merges → write up the RCA. Today this takes 30–60 min per ticket. **sla-crushers** automates the loop end-to-end and posts the RCA back in the Slack thread, with every query and code reference persisted to a SQLite audit trail so a reviewer can verify the work.

## What it does

```
Slack link  ─►  fetch thread + ClickUp ticket
            ─►  classify: (platform, module, country, identifier, time_window, problem)
            ─►  resolve target via temporal_v2.registry  ─► runner config + spider + items
            ─►  agent loop (Snowflake / Postgres / repo grep / S3 / Temporal / git / GitHub PRs)
            ─►  structured RCA (evidence-cited, ruled-out hypotheses, chain-of-custody)
            ─►  optional draft PR for one of: selector / header / pincode / URL pattern
            ─►  post in the Slack thread
```

## Guardrails

- Snowflake tool only allows `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN`. Anything else hard-rejects.
- Postgres tool wraps every query in `SET TRANSACTION READ ONLY`.
- Every tool call is persisted to `runs/logs/runs.db` (SQLite) — query inputs, results, latency, errors.
- Auto-PR is **draft only**. The agent never auto-merges.
- Tool budget per run is bounded by `RCA_TOOL_BUDGET` (default 25).

## Setup

```bash
git clone git@github.com:robinmishraa/sla-crushers.git
cd sla-crushers
cp .env.example .env
# Fill in tokens AND set SCRAPING_REPO_ROOT to your local clone of GobbleCube/scraping

poetry install
poetry run uvicorn rca_agent.app.main:app --reload --port 8765
# Open http://localhost:8765 and paste a Slack message link
```

The agent depends on a local clone of the scraping repo (`SCRAPING_REPO_ROOT`) for:
- runner configs (`runners/<platform>/<module>.py`)
- the registry (`temporal_v2/registry.py`)
- the existing Snowflake client (`temporal/resources/snowflake_client.py`)
- the existing Postgres engine (`common/config.py`)
- spider source (`scraping/spiders/`, `scraping/items/`)
- git history + GitHub PRs touching those paths

## Architecture

```
rca_agent/
├── app/                    # FastAPI app + UI + Slack /rca slash command
├── core/
│   ├── orchestrator.py     # the state machine: bootstrap → investigate → submit → post
│   ├── llm.py              # Anthropic client wrapper
│   ├── schemas.py          # Pydantic contracts (Issue, Evidence, Hypothesis, RcaReport, …)
│   ├── tool_registry.py    # registers every tool the agent can call
│   ├── settings.py         # env var loader + sys.path injection for the scraping repo
│   └── prompts/            # system + extract_issue + compose_rca + propose_fix
├── tools/                  # one module per capability: SF, PG, repo, registry, git, GH, S3, Temporal, Slack, ClickUp
├── fix_recipes/            # deterministic generators for the four allowed fix kinds
└── runs/                   # SQLite audit log
```

## Status

Hackathon WIP. See the issues / project board for what's wired vs. stubbed.
