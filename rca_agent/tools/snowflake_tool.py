"""Snowflake tools.

Reuses the existing scraping repo's `temporal.resources.snowflake_client.SnowflakeResource`
for connection + key-pair auth, but wraps every query in a strict read-only guard:

  - Only SELECT / WITH / SHOW / DESCRIBE / DESC / EXPLAIN are allowed.
  - A statement timeout is enforced on the cursor.
  - A LIMIT is auto-injected on bare SELECTs.
  - Multi-statement is rejected.

This is the single most important guardrail in the whole agent. Do not loosen it.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


_ALLOWED_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")
_FORBIDDEN_TOKENS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "GRANT", "REVOKE", "COPY", "PUT", "REMOVE", "CALL",
    "USE ROLE", "USE WAREHOUSE",
)


def _strip_sql(sql: str) -> str:
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    return s.strip().rstrip(";").strip()


def _validate_read_only(sql: str) -> str:
    cleaned = _strip_sql(sql)
    if not cleaned:
        raise ToolError("Empty SQL.")
    if ";" in cleaned:
        raise ToolError("Multi-statement SQL is not allowed.")
    upper = cleaned.upper()
    if not any(upper.startswith(p) for p in _ALLOWED_PREFIXES):
        raise ToolError(
            f"Only {_ALLOWED_PREFIXES} statements are allowed. Got: {upper.split()[0]!r}"
        )
    for bad in _FORBIDDEN_TOKENS:
        if re.search(rf"\b{bad}\b", upper):
            raise ToolError(f"Forbidden keyword in SQL: {bad}")
    return cleaned


def _maybe_inject_limit(sql: str, default_limit: int) -> str:
    upper = sql.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return sql
    if re.search(r"\bLIMIT\s+\d+\b", upper):
        return sql
    return f"{sql}\nLIMIT {default_limit}"


def _get_resource():
    """Lazy import so missing env vars surface only when first used."""
    from temporal.resources.snowflake_client import SnowflakeResource  # noqa: WPS433
    return SnowflakeResource()


def _execute(sql: str, default_limit: int | None) -> dict[str, Any]:
    cleaned = _validate_read_only(sql)
    if default_limit:
        cleaned = _maybe_inject_limit(cleaned, default_limit)

    resource = _get_resource()
    conn = resource.connect()
    timeout = settings().SF_STATEMENT_TIMEOUT_S
    with conn.cursor() as cursor:
        try:
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout}")
        except Exception as e:
            logger.warning("could not set Snowflake statement timeout: {}", e)
        cursor.execute(cleaned)
        cols = [c[0] for c in (cursor.description or [])]
        rows = cursor.fetchmany(max(default_limit or 0, 200) + 1)
    truncated = bool(default_limit) and len(rows) > default_limit
    rows = rows[:default_limit] if default_limit else rows
    payload = [dict(zip(cols, r)) for r in rows]
    return {
        "sql": cleaned,
        "columns": cols,
        "rows": payload,
        "rows_count": len(payload),
        "truncated": truncated,
    }


# ---------- Tools ----------

class SnowflakeQueryTool(BaseTool):
    name = ToolName.SF_QUERY
    description = (
        "Run a READ-ONLY SQL query on Snowflake. "
        "Allowed: SELECT, WITH, SHOW, DESCRIBE, EXPLAIN. Anything else is rejected. "
        "If you don't include a LIMIT, one will be injected. "
        "Always qualify tables with database.schema. Prefer COUNT(*) and small samples first; only widen the search after the schema is known."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Single SQL statement."},
            "limit": {
                "type": "integer",
                "description": "Max rows. Defaults to RCA_SF_DEFAULT_LIMIT.",
            },
        },
        "required": ["sql"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        sql = args["sql"]
        limit = int(args.get("limit") or settings().SF_DEFAULT_LIMIT)
        return _execute(sql, default_limit=limit)


class SnowflakeListTablesTool(BaseTool):
    name = ToolName.SF_LIST_TABLES
    description = (
        "List tables in a Snowflake schema. Use this once at the start of an "
        "investigation to discover what tables exist for a platform/module."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "database": {"type": "string"},
            "schema": {"type": "string"},
            "name_like": {"type": "string", "description": "Optional ILIKE pattern."},
        },
        "required": ["database", "schema"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        db = args["database"]
        sc = args["schema"]
        like = args.get("name_like")
        sql = f"SHOW TABLES IN SCHEMA {db}.{sc}"
        if like:
            sql += f" LIKE '{like}'"
        return _execute(sql, default_limit=500)


class SnowflakeDescribeTool(BaseTool):
    name = ToolName.SF_DESCRIBE
    description = "Describe a Snowflake table: columns, types, comments."
    input_schema = {
        "type": "object",
        "properties": {
            "fully_qualified_table": {
                "type": "string",
                "description": "e.g. SCRAPING.PUBLIC.BLINKIT_SEARCH",
            },
        },
        "required": ["fully_qualified_table"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        return _execute(f"DESCRIBE TABLE {args['fully_qualified_table']}", default_limit=500)
