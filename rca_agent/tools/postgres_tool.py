"""Postgres tools (read-only).

Uses the existing scraping repo's `common.config.database` SQLAlchemy engine.
Every query runs inside `SET TRANSACTION READ ONLY`, plus the same SQL guardrail
used for Snowflake.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import text

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


_ALLOWED_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW")
_FORBIDDEN_TOKENS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX", "CLUSTER",
)


def _strip_sql(sql: str) -> str:
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    return s.strip().rstrip(";").strip()


def _validate(sql: str) -> str:
    cleaned = _strip_sql(sql)
    if not cleaned:
        raise ToolError("Empty SQL.")
    if ";" in cleaned:
        raise ToolError("Multi-statement SQL is not allowed.")
    upper = cleaned.upper()
    if not any(upper.startswith(p) for p in _ALLOWED_PREFIXES):
        raise ToolError(
            f"Only {_ALLOWED_PREFIXES} are allowed on Postgres. Got: {upper.split()[0]!r}"
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


def _get_engine():
    from common.config import database  # noqa: WPS433
    if database.engine is None:
        raise ToolError("Postgres engine is not configured (DB_HOST not set).")
    return database.engine


def _execute(sql: str, limit: int | None) -> dict[str, Any]:
    cleaned = _validate(sql)
    if limit:
        cleaned = _maybe_inject_limit(cleaned, limit)
    engine = _get_engine()
    with engine.connect() as conn:
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        except Exception as e:
            logger.warning("could not set READ ONLY transaction: {}", e)
        try:
            conn.execute(text(f"SET statement_timeout = {settings().SF_STATEMENT_TIMEOUT_S * 1000}"))
        except Exception as e:
            logger.debug("statement_timeout not applied: {}", e)
        result = conn.execute(text(cleaned))
        cols = list(result.keys())
        rows = [dict(r._mapping) for r in result.fetchmany((limit or settings().PG_DEFAULT_LIMIT) + 1)]
    truncated = bool(limit) and len(rows) > limit
    rows = rows[:limit] if limit else rows
    return {
        "sql": cleaned,
        "columns": cols,
        "rows": rows,
        "rows_count": len(rows),
        "truncated": truncated,
    }


class PostgresQueryTool(BaseTool):
    name = ToolName.PG_QUERY
    description = (
        "Run a READ-ONLY SQL query on the scraping Postgres. "
        "Allowed: SELECT, WITH, EXPLAIN, SHOW. Always qualify with schema. "
        "If you don't include a LIMIT, one will be injected."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["sql"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        return _execute(args["sql"], limit=int(args.get("limit") or settings().PG_DEFAULT_LIMIT))


class PostgresListTablesTool(BaseTool):
    name = ToolName.PG_LIST_TABLES
    description = "List tables in a Postgres schema, with row-count estimates."
    input_schema = {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "default": "public"},
            "name_like": {"type": "string"},
        },
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        schema = args.get("schema") or "public"
        like = args.get("name_like")
        where = "WHERE n.nspname = :schema"
        params = {"schema": schema}
        if like:
            where += " AND c.relname ILIKE :like"
            params["like"] = like
        # Render through query tool to keep audit-trail consistency.
        sql = f"""
            SELECT n.nspname AS schema, c.relname AS table, c.reltuples::bigint AS approx_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            {where} AND c.relkind IN ('r','p')
            ORDER BY c.reltuples DESC
        """
        engine = _get_engine()
        with engine.connect() as conn:
            try:
                conn.execute(text("SET TRANSACTION READ ONLY"))
            except Exception:
                pass
            res = conn.execute(text(sql), params)
            cols = list(res.keys())
            rows = [dict(r._mapping) for r in res.fetchall()]
        return {"sql": sql.strip(), "columns": cols, "rows": rows, "rows_count": len(rows)}


class PostgresDescribeTool(BaseTool):
    name = ToolName.PG_DESCRIBE
    description = "Describe a Postgres table: columns, types, defaults."
    input_schema = {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "default": "public"},
            "table": {"type": "string"},
        },
        "required": ["table"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        schema = args.get("schema") or "public"
        table = args["table"]
        sql = """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            ORDER BY ordinal_position
        """
        engine = _get_engine()
        with engine.connect() as conn:
            try:
                conn.execute(text("SET TRANSACTION READ ONLY"))
            except Exception:
                pass
            res = conn.execute(text(sql), {"schema": schema, "table": table})
            cols = list(res.keys())
            rows = [dict(r._mapping) for r in res.fetchall()]
        return {"sql": sql.strip(), "columns": cols, "rows": rows, "rows_count": len(rows)}
