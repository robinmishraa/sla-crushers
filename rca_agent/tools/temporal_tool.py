"""Temporal tool: recent workflow executions for a (platform, module).

Connects to the configured Temporal namespace and returns the last N
ScrapeJobWorkflow executions, with status, run duration, and child stats.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from rca_agent.core.schemas import ToolName
from rca_agent.tools.base import BaseTool, ToolError


def _connect():
    from temporalio.client import Client  # noqa: WPS433
    host = os.getenv("TEMPORAL_HOST")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY")
    if not host:
        raise ToolError("TEMPORAL_HOST is not set.")
    return Client.connect(
        host,
        namespace=namespace,
        api_key=api_key,
        tls=bool(api_key),
    )


class TemporalHistoryTool(BaseTool):
    name = ToolName.TEMPORAL_HISTORY
    description = (
        "List recent ScrapeJobWorkflow executions, optionally filtered to a "
        "platform/module. Returns status, start/close times, and run id."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "platform": {"type": "string"},
            "module": {"type": "string"},
            "max_results": {"type": "integer", "default": 20},
            "since_hours": {"type": "integer", "default": 48},
        },
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return asyncio.run(self._async_execute(args))
        except RuntimeError:
            # If we're already in an event loop (rare in this app), use a new one.
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._async_execute(args))
            finally:
                loop.close()

    async def _async_execute(self, args: dict[str, Any]) -> dict[str, Any]:
        client = await _connect()
        max_results = int(args.get("max_results") or 20)
        since_hours = int(args.get("since_hours") or 48)
        platform = args.get("platform")
        module = args.get("module")

        query_parts = [
            "WorkflowType IN ('ScrapeJobWorkflow', 'OnDemandScraping')",
            f"StartTime > '{_iso_hours_ago(since_hours)}'",
        ]
        # Search attribute names depend on the deployment; this best-effort
        # filter is wrapped in try/except below.
        if platform:
            query_parts.append(f"PlatformAttribute = '{platform}'")
        if module:
            query_parts.append(f"ModuleAttribute = '{module}'")

        query = " AND ".join(query_parts)

        executions: list[dict[str, Any]] = []
        try:
            async for ex in client.list_workflows(query):
                executions.append(
                    {
                        "workflow_id": ex.id,
                        "run_id": ex.run_id,
                        "type": ex.workflow_type,
                        "status": ex.status.name if ex.status else None,
                        "start_time": ex.start_time.isoformat() if ex.start_time else None,
                        "close_time": ex.close_time.isoformat() if ex.close_time else None,
                        "task_queue": ex.task_queue,
                    }
                )
                if len(executions) >= max_results:
                    break
        except Exception as e:
            # Fallback: return diagnostic info instead of failing the whole RCA.
            return {
                "executions": [],
                "rows_count": 0,
                "warning": f"Temporal list_workflows failed: {e}",
                "query_attempted": query,
            }

        return {"executions": executions, "rows_count": len(executions), "query": query}


def _iso_hours_ago(hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
