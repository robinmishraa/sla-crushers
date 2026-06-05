"""One-shot CLI: `python -m rca_agent.cli <slack_url>`.

Useful for testing the agent without spinning up the web app.
Streams events to stdout and writes the final RCA to runs/<run_id>.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from loguru import logger

from rca_agent.core.orchestrator import run_rca
from rca_agent.core.settings import settings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m rca_agent.cli <slack_message_url>", file=sys.stderr)
        return 2
    slack_url = argv[1]
    final = None
    for event in run_rca(slack_url):
        et = event.type
        if et == "status":
            logger.info("• {}", event.payload.get("message", ""))
        elif et == "tool_call_started":
            logger.info("→ {} {}", event.payload.get("tool"), event.payload.get("args"))
        elif et == "tool_call_finished":
            ok = event.payload.get("ok")
            rows = event.payload.get("rows")
            logger.info("{} {} ({})", "✓" if ok else "✗", event.payload.get("tool"),
                        f"{rows} rows" if rows is not None else "")
        elif et == "issue_extracted":
            logger.info("issue: {}", event.payload.get("issue"))
        elif et == "rca_complete":
            final = event.payload.get("rca")
            logger.success("RCA complete (run_id={})", event.run_id)
        elif et == "error":
            logger.error("error: {}", event.payload.get("error"))
    if final:
        out = settings().PROJECT_ROOT / "runs" / f"{final.get('run_id', 'last')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(final, indent=2, default=str), encoding="utf-8")
        logger.info("written: {}", out)
        print(final.get("summary", ""))
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
