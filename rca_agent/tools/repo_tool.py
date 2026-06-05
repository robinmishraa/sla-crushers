"""Repo tools: ripgrep + read scoped to the scraping codebase.

Search is scoped to the directories where the agent legitimately needs to look
(runners, spiders, items, common). This bounds the LLM's exploration and
prevents wandering into irrelevant fixtures or dags assets dumps.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


_DEFAULT_INCLUDE_DIRS = [
    "runners",
    "scraping/spiders",
    "scraping/items",
    "scraping/middlewares.py",
    "scraping/settings.py",
    "common",
    "temporal",
    "temporal_v2",
    "models",
    "dags/assets",
]

_MAX_FILE_BYTES = 200_000  # 200 KB read cap per file


def _scraping_root() -> Path:
    try:
        return settings().require_scraping_repo()
    except RuntimeError as e:
        raise ToolError(str(e)) from e


def _resolve_in_repo(rel: str) -> Path:
    root = _scraping_root()
    p = (root / rel).resolve()
    repo = root.resolve()
    if not str(p).startswith(str(repo)):
        raise ToolError(f"Path escapes scraping repo: {rel}")
    return p


class RepoSearchTool(BaseTool):
    name = ToolName.REPO_SEARCH
    description = (
        "Search the scraping codebase for a regex pattern using ripgrep. "
        "Defaults to scoped dirs: runners/, scraping/spiders/, scraping/items/, "
        "common/, temporal/, temporal_v2/, models/, dags/assets/. "
        "Use this to find selectors, headers, URLs, runner configs, etc."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex (rg syntax)."},
            "path": {
                "type": "string",
                "description": "Optional sub-path, relative to repo root.",
            },
            "glob": {"type": "string", "description": "Optional --glob filter, e.g. '*.py'."},
            "max_results": {"type": "integer", "default": 80},
            "case_insensitive": {"type": "boolean", "default": False},
        },
        "required": ["pattern"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args["pattern"]
        max_results = int(args.get("max_results") or 80)
        cmd = ["rg", "--no-heading", "--line-number", "--with-filename", "-S"]
        if args.get("case_insensitive"):
            cmd.append("-i")
        if args.get("glob"):
            cmd += ["--glob", args["glob"]]
        cmd += ["-m", str(max_results), pattern]
        targets: list[str] = []
        if args.get("path"):
            targets = [str(_resolve_in_repo(args["path"]))]
        else:
            scraping_root = _scraping_root()
            targets = [
                str(_resolve_in_repo(d))
                for d in _DEFAULT_INCLUDE_DIRS
                if (scraping_root / d).exists()
            ]
        cmd += targets
        try:
            out = subprocess.run(
                cmd,
                cwd=str(_scraping_root()),
                capture_output=True,
                text=True,
                timeout=20,
            )
        except FileNotFoundError as e:
            raise ToolError("ripgrep ('rg') is not installed.") from e

        if out.returncode not in (0, 1):
            raise ToolError(f"ripgrep failed: {out.stderr[:500]}")

        hits: list[dict[str, Any]] = []
        root_resolved = _scraping_root().resolve()
        for line in (out.stdout or "").splitlines():
            try:
                file_part, line_part, match_part = line.split(":", 2)
            except ValueError:
                continue
            try:
                rel = str(Path(file_part).resolve().relative_to(root_resolved))
            except ValueError:
                rel = file_part
            hits.append({"file": rel, "line": int(line_part), "match": match_part.rstrip()})
            if len(hits) >= max_results:
                break

        return {"command": shlex.join(cmd), "hits": hits, "rows_count": len(hits)}


class RepoReadTool(BaseTool):
    name = ToolName.REPO_READ
    description = (
        "Read a slice of a file from the scraping codebase by line range. "
        "Use after repo.search to read the surrounding context of a hit."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to repo root."},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {
                "type": "integer",
                "description": "Inclusive. Defaults to start_line + 80.",
            },
        },
        "required": ["path"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        rel = args["path"]
        start = max(1, int(args.get("start_line") or 1))
        end_arg = args.get("end_line")
        end = int(end_arg) if end_arg else start + 80
        p = _resolve_in_repo(rel)
        if not p.exists() or not p.is_file():
            raise ToolError(f"File not found: {rel}")
        if p.stat().st_size > _MAX_FILE_BYTES:
            content = p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
            note = f"File truncated to first {_MAX_FILE_BYTES} bytes."
        else:
            content = p.read_text(encoding="utf-8", errors="replace")
            note = ""
        lines = content.splitlines()
        sliced = lines[start - 1 : end]
        numbered = "\n".join(f"{start + i:>5} | {ln}" for i, ln in enumerate(sliced))
        return {
            "path": rel,
            "start_line": start,
            "end_line": start + len(sliced) - 1,
            "total_lines": len(lines),
            "content": numbered,
            "note": note,
            "rows_count": len(sliced),
        }
