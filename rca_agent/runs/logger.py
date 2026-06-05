"""SQLite audit logger.

Every tool call the agent makes is written here so a reviewer can:
  - replay the exact Snowflake/Postgres queries the agent ran,
  - see the full input/output for any tool call,
  - audit cost (LLM tokens) and latency per run.

The audit trail is the entire reason the RCA is "convincing" — without it
the LLM is just a confident narrator. With it, the LLM is a researcher
whose homework is on the table.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rca_agent.core.settings import settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    slack_url TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    rca_json TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    error TEXT,
    result_preview TEXT,
    rows INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
"""


class AuditLogger:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or (settings().LOG_DIR / "runs.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def start_run(self, slack_url: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(run_id, slack_url, started_at) VALUES (?, ?, ?)",
                (run_id, slack_url, self._now()),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, rca: Optional[dict[str, Any]] = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, rca_json=? WHERE run_id=?",
                (self._now(), status, json.dumps(rca) if rca is not None else None, run_id),
            )

    def start_tool_call(self, run_id: str, tool: str, args: dict[str, Any]) -> str:
        call_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls(id, run_id, tool, args_json, started_at) VALUES (?, ?, ?, ?, ?)",
                (call_id, run_id, tool, json.dumps(args, default=str)[:8000], self._now()),
            )
        return call_id

    def finish_tool_call(
        self,
        call_id: str,
        ok: bool,
        result_preview: str | None = None,
        rows: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tool_calls
                SET finished_at=?, ok=?, result_preview=?, rows=?, error=?
                WHERE id=?
                """,
                (self._now(), 1 if ok else 0, (result_preview or "")[:4000], rows, error, call_id),
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                return None
            calls = conn.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY started_at",
                (run_id,),
            ).fetchall()
            return {"run": dict(row), "tool_calls": [dict(c) for c in calls]}


_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _logger
    if _logger is None:
        _logger = AuditLogger()
    return _logger
