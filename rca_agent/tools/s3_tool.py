"""S3 tools: list and read raw response artifacts.

The scraping pipeline writes raw upstream responses (HTML, JSON) to S3 under
a path keyed by platform/module/run. Reading the raw bytes is often the
fastest way to confirm 'spider got X but pipeline stored Y'.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


def _client():
    return boto3.client("s3")


def _default_bucket() -> str:
    import os
    bucket = os.getenv("AWS_S3_BUCKET", "")
    if not bucket:
        raise ToolError("AWS_S3_BUCKET is not set.")
    return bucket


_MAX_BYTES = 200_000


class S3ListTool(BaseTool):
    name = ToolName.S3_LIST
    description = (
        "List S3 objects under a prefix. Use this to find raw response artifacts "
        "for a specific platform/module/run. Defaults to the configured AWS_S3_BUCKET."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prefix": {"type": "string"},
            "bucket": {"type": "string"},
            "max_keys": {"type": "integer", "default": 100},
        },
        "required": ["prefix"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        bucket = args.get("bucket") or _default_bucket()
        try:
            resp = _client().list_objects_v2(
                Bucket=bucket,
                Prefix=args["prefix"],
                MaxKeys=int(args.get("max_keys") or 100),
            )
        except (ClientError, BotoCoreError) as e:
            raise ToolError(f"S3 list failed: {e}") from e
        keys = [
            {"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()}
            for o in resp.get("Contents", [])
        ]
        return {"bucket": bucket, "prefix": args["prefix"], "keys": keys, "rows_count": len(keys)}


class S3ReadTool(BaseTool):
    name = ToolName.S3_READ
    description = (
        "Read up to ~200KB of an S3 object. JSON is parsed and returned; other "
        "content is returned as text (UTF-8 best-effort)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "bucket": {"type": "string"},
        },
        "required": ["key"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        bucket = args.get("bucket") or _default_bucket()
        key = args["key"]
        try:
            obj = _client().get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{_MAX_BYTES - 1}")
        except (ClientError, BotoCoreError) as e:
            raise ToolError(f"S3 get failed: {e}") from e
        body = obj["Body"].read()
        as_text = body.decode("utf-8", errors="replace")
        parsed: Any = None
        if key.endswith(".json") or key.endswith(".jsonl"):
            try:
                parsed = json.loads(as_text)
            except Exception:
                parsed = None
        return {
            "bucket": bucket,
            "key": key,
            "bytes": len(body),
            "truncated": len(body) >= _MAX_BYTES,
            "text": as_text if parsed is None else None,
            "parsed_json": parsed,
            "rows_count": 1,
        }
