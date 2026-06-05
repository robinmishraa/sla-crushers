"""ClickUp tool: fetch a ticket's title, description, status, and recent comments.

A ClickUp task URL looks like:
  https://app.clickup.com/t/<task_id>
or with workspace prefix:
  https://app.clickup.com/t/<workspace_id>/<task_id>
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


_TASK_ID_RE = re.compile(r"clickup\.com/t/(?:[A-Za-z0-9]+/)?(?P<id>[A-Za-z0-9_\-]+)")


def parse_clickup_url(url: str) -> str:
    m = _TASK_ID_RE.search(url.strip())
    if not m:
        raise ToolError(f"Not a valid ClickUp task URL: {url!r}")
    return m.group("id")


class ClickupGetTicketTool(BaseTool):
    name = ToolName.CLICKUP_GET_TICKET
    description = (
        "Fetch a ClickUp task by URL or task id. Returns title, status, description, "
        "creator, assignees, and the last ~20 comments. Use this to read the original "
        "ticket linked from the Slack message."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url_or_id": {"type": "string", "description": "ClickUp task URL or raw task id."},
            "include_comments": {"type": "boolean", "default": True},
        },
        "required": ["url_or_id"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        token = settings().CLICKUP_API_TOKEN
        if not token:
            raise ToolError("CLICKUP_API_TOKEN is not set.")

        raw = args["url_or_id"]
        task_id = parse_clickup_url(raw) if raw.startswith("http") else raw
        headers = {"Authorization": token, "Accept": "application/json"}
        with httpx.Client(timeout=15.0, headers=headers) as client:
            r = client.get(f"https://api.clickup.com/api/v2/task/{task_id}")
            if r.status_code != 200:
                raise ToolError(f"ClickUp task fetch failed [{r.status_code}]: {r.text[:200]}")
            task = r.json()

            comments: list[dict[str, Any]] = []
            if args.get("include_comments", True):
                rc = client.get(f"https://api.clickup.com/api/v2/task/{task_id}/comment")
                if rc.status_code == 200:
                    for c in (rc.json().get("comments") or [])[:20]:
                        comments.append(
                            {
                                "id": c.get("id"),
                                "user": (c.get("user") or {}).get("username"),
                                "date": c.get("date"),
                                "text": c.get("comment_text") or "",
                            }
                        )

        return {
            "task_id": task.get("id"),
            "name": task.get("name"),
            "status": (task.get("status") or {}).get("status"),
            "url": task.get("url"),
            "description": (task.get("description") or task.get("text_content") or "")[:8000],
            "creator": (task.get("creator") or {}).get("username"),
            "assignees": [a.get("username") for a in task.get("assignees", []) or []],
            "tags": [t.get("name") for t in task.get("tags", []) or []],
            "date_created": task.get("date_created"),
            "date_updated": task.get("date_updated"),
            "priority": (task.get("priority") or {}).get("priority"),
            "list": (task.get("list") or {}).get("name"),
            "folder": (task.get("folder") or {}).get("name"),
            "space": (task.get("space") or {}).get("id"),
            "comments": comments,
            "rows_count": 1,
        }
