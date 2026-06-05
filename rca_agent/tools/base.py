"""Base classes for agent tools.

Every tool exposes an Anthropic-compatible JSON schema and a single `run()`
method. The base class handles audit logging so each concrete tool only has
to implement the actual work.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from rca_agent.core.schemas import ToolName
from rca_agent.runs.logger import get_audit_logger


class ToolError(RuntimeError):
    """Raised by tools when an input is invalid or a hard guardrail is hit."""


class BaseTool(ABC):
    name: ToolName
    description: str
    input_schema: dict[str, Any]

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @abstractmethod
    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        """Concrete tool work. Raise ToolError for clean validation errors."""

    def run(self, args: dict[str, Any], run_id: str) -> dict[str, Any]:
        audit = get_audit_logger()
        call_id = audit.start_tool_call(run_id, self.name.value, args)
        try:
            result = self._execute(args)
            preview = self._preview(result)
            rows = result.get("rows_count") if isinstance(result, dict) else None
            audit.finish_tool_call(call_id, ok=True, result_preview=preview, rows=rows)
            result["_tool_call_id"] = call_id
            return result
        except Exception as e:
            logger.exception("tool {} failed: {}", self.name.value, e)
            audit.finish_tool_call(call_id, ok=False, error=str(e))
            return {"error": str(e), "_tool_call_id": call_id}

    @staticmethod
    def _preview(result: dict[str, Any]) -> str:
        try:
            return json.dumps(result, default=str)[:4000]
        except Exception:
            return str(result)[:4000]
