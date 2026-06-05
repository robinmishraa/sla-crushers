"""Smoke test: imports every module and runs a couple of guardrail checks.

Run:  python scripts/smoke_test.py
Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    failures: list[str] = []

    _section("settings")
    try:
        from rca_agent.core.settings import settings
        s = settings()
        print(f"PROJECT_ROOT       = {s.PROJECT_ROOT}")
        print(f"SCRAPING_REPO_ROOT = {s.SCRAPING_REPO_ROOT}")
        print(f"LOG_DIR            = {s.LOG_DIR}")
        assert s.SCRAPING_REPO_ROOT.exists(), "scraping repo path does not exist"
    except Exception as e:
        failures.append(f"settings: {e}")
        print(f"FAIL: {e}")

    _section("schemas")
    try:
        from rca_agent.core.schemas import RcaReport, Issue, Hypothesis  # noqa: F401
        print("ok")
    except Exception as e:
        failures.append(f"schemas: {e}")
        print(f"FAIL: {e}")

    _section("tool registry")
    try:
        from rca_agent.core.tool_registry import build_investigation_tools
        tools = build_investigation_tools()
        print(f"registered {len(tools)} tools:")
        for name in sorted(tools.keys()):
            print(f"  • {name}")
    except Exception as e:
        failures.append(f"tool_registry: {e}")
        print(f"FAIL: {e}")

    _section("Snowflake guardrail")
    try:
        from rca_agent.tools.snowflake_tool import _validate_read_only
        from rca_agent.tools.base import ToolError
        _validate_read_only("SELECT 1")
        try:
            _validate_read_only("DROP TABLE foo")
            failures.append("Snowflake guardrail let DROP through")
            print("FAIL: DROP should have been rejected")
        except ToolError:
            print("ok — DROP rejected")
        try:
            _validate_read_only("SELECT 1; SELECT 2")
            failures.append("Snowflake guardrail let multi-statement through")
            print("FAIL: multi-statement should have been rejected")
        except ToolError:
            print("ok — multi-statement rejected")
    except Exception as e:
        failures.append(f"snowflake guardrail: {e}")
        print(f"FAIL: {e}")

    _section("Slack link parser")
    try:
        from rca_agent.tools.slack_tool import parse_slack_link
        url = "https://gobblecube.slack.com/archives/C012ABCDEF/p1700000000123456"
        parsed = parse_slack_link(url)
        assert parsed.channel == "C012ABCDEF", parsed.channel
        assert parsed.ts == "1700000000.123456", parsed.ts
        print(f"ok — {parsed.channel} {parsed.ts}")
    except Exception as e:
        failures.append(f"slack parser: {e}")
        print(f"FAIL: {e}")

    _section("registry resolution")
    try:
        from rca_agent.tools.registry_tool import _all_pairs
        pairs = _all_pairs()
        print(f"ok — {len(pairs)} (platform, module) pairs registered")
        if pairs:
            print(f"  e.g. {pairs[0]}")
    except Exception as e:
        failures.append(f"registry: {e}")
        print(f"FAIL: {e}")

    _section("audit logger")
    try:
        from rca_agent.runs.logger import get_audit_logger
        log = get_audit_logger()
        rid = log.start_run("smoke://test")
        cid = log.start_tool_call(rid, "smoke.test", {"hello": "world"})
        log.finish_tool_call(cid, ok=True, result_preview="{}", rows=0)
        log.finish_run(rid, "ok")
        loaded = log.get_run(rid)
        assert loaded and loaded["run"]["run_id"] == rid
        print(f"ok — audit log path: {log.db_path}")
    except Exception as e:
        failures.append(f"audit logger: {e}")
        print(f"FAIL: {e}")

    _section("orchestrator import")
    try:
        from rca_agent.core.orchestrator import run_rca  # noqa: F401
        print("ok")
    except Exception as e:
        failures.append(f"orchestrator: {e}")
        print(f"FAIL: {e}")

    _section("FastAPI app import")
    try:
        from rca_agent.app.main import app  # noqa: F401
        print("ok")
    except Exception as e:
        failures.append(f"app: {e}")
        print(f"FAIL: {e}")

    print()
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
