"""Slack tools: parse a permalink, fetch thread context, post a reply.

A Slack permalink looks like:
  https://<workspace>.slack.com/archives/<CHANNEL>/p<TS_NO_DOT>
or with a thread parent:
  https://<workspace>.slack.com/archives/<CHANNEL>/p<TS>?thread_ts=<TS>&cid=<CHANNEL>

`p1700000000123456` means timestamp `1700000000.123456`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


_PERMALINK_RE = re.compile(
    r"https?://[\w\-]+\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d+)"
)


@dataclass
class ParsedSlackLink:
    channel: str
    ts: str
    thread_ts: str | None = None

    @property
    def parent_ts(self) -> str:
        return self.thread_ts or self.ts


def parse_slack_link(url: str) -> ParsedSlackLink:
    """Convert a Slack permalink to (channel, message_ts, [thread_ts])."""
    m = _PERMALINK_RE.search(url.strip())
    if not m:
        raise ToolError(f"Not a valid Slack permalink: {url!r}")
    raw_ts = m.group("ts")
    if len(raw_ts) <= 6:
        raise ToolError(f"Slack ts looks malformed: {raw_ts!r}")
    ts = f"{raw_ts[:-6]}.{raw_ts[-6:]}"

    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    thread_ts = qs.get("thread_ts", [None])[0]
    return ParsedSlackLink(channel=m.group("channel"), ts=ts, thread_ts=thread_ts)


_CLICKUP_RE = re.compile(r"https?://app\.clickup\.com/t/[A-Za-z0-9_\-]+")


def extract_clickup_urls(text: str) -> list[str]:
    if not text:
        return []
    return list(dict.fromkeys(_CLICKUP_RE.findall(text)))


def _client() -> WebClient:
    if not settings().SLACK_BOT_TOKEN:
        raise ToolError("SLACK_BOT_TOKEN is not set.")
    return WebClient(token=settings().SLACK_BOT_TOKEN)


# ---------- Tools ----------

class SlackGetThreadTool(BaseTool):
    name = ToolName.SLACK_GET_THREAD
    description = (
        "Given a Slack message permalink, return the root message + all replies in the thread. "
        "Also returns any ClickUp URLs found in the message text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Slack message permalink."},
        },
        "required": ["url"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url", "")
        parsed = parse_slack_link(url)
        client = _client()
        try:
            resp = client.conversations_replies(
                channel=parsed.channel,
                ts=parsed.parent_ts,
                limit=200,
                inclusive=True,
            )
        except SlackApiError as e:
            err = e.response.get("error", str(e))
            if err in {"thread_not_found", "message_not_found"}:
                resp = client.conversations_history(
                    channel=parsed.channel,
                    latest=parsed.ts,
                    oldest=parsed.ts,
                    limit=1,
                    inclusive=True,
                )
            else:
                raise ToolError(f"Slack API error: {err}") from e

        messages = resp.get("messages", [])
        clickup_urls: list[str] = []
        clean_messages: list[dict[str, Any]] = []
        for m in messages:
            text = m.get("text", "")
            clickup_urls.extend(extract_clickup_urls(text))
            clean_messages.append(
                {
                    "user": m.get("user"),
                    "ts": m.get("ts"),
                    "text": text,
                    "reactions": [r.get("name") for r in m.get("reactions", []) or []],
                }
            )
        return {
            "channel": parsed.channel,
            "parent_ts": parsed.parent_ts,
            "messages": clean_messages,
            "clickup_urls": list(dict.fromkeys(clickup_urls)),
            "rows_count": len(clean_messages),
        }


class SlackPostReplyTool(BaseTool):
    name = ToolName.SLACK_POST_REPLY
    description = (
        "Post a reply in the thread of a Slack message. Use this only once per run, "
        "to deliver the final RCA back to the user."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "thread_ts": {"type": "string"},
            "text": {"type": "string", "description": "Plain text fallback."},
            "blocks": {
                "type": "array",
                "description": "Optional Slack Block Kit blocks. If omitted, posts plain text.",
                "items": {"type": "object"},
            },
        },
        "required": ["channel", "thread_ts", "text"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        client = _client()
        try:
            resp = client.chat_postMessage(
                channel=args["channel"],
                thread_ts=args["thread_ts"],
                text=args["text"],
                blocks=args.get("blocks"),
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as e:
            raise ToolError(f"Slack post failed: {e.response.get('error', str(e))}") from e
        return {
            "ok": True,
            "channel": resp.get("channel"),
            "ts": resp.get("ts"),
            "permalink": None,  # Slack does not return permalink directly
        }


def post_rca_blocks(channel: str, thread_ts: str, summary: str, blocks: list[dict]) -> dict:
    """Convenience used by the orchestrator to post the final RCA."""
    return SlackPostReplyTool().run(
        {"channel": channel, "thread_ts": thread_ts, "text": summary, "blocks": blocks},
        run_id="post-final",
    )


def get_thread_for_link(url: str, run_id: str) -> dict[str, Any]:
    return SlackGetThreadTool().run({"url": url}, run_id=run_id)
