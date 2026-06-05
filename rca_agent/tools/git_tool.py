"""Git tools: log, show, blame.

Lets the agent backtrack regressions to the exact commit/PR that introduced
them. Critical to satisfy the 'check old PRs' requirement.
"""
from __future__ import annotations

import subprocess
from typing import Any

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


def _git(*args: str, max_bytes: int = 200_000) -> str:
    try:
        repo_root = settings().require_scraping_repo()
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as e:
        raise ToolError("git is not installed.") from e
    if out.returncode != 0:
        raise ToolError(f"git failed: {out.stderr.strip()[:500]}")
    text = out.stdout or ""
    if len(text) > max_bytes:
        text = text[:max_bytes] + f"\n... [truncated at {max_bytes} bytes]"
    return text


class GitLogTool(BaseTool):
    name = ToolName.GIT_LOG
    description = (
        "Show recent commits, optionally filtered to a path or time window. "
        "Use this to find recent changes to a runner/spider that might have "
        "introduced the regression."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Optional path filter (file or dir)."},
            "since": {"type": "string", "description": "git --since (e.g. '14 days ago')."},
            "until": {"type": "string", "description": "git --until."},
            "max_count": {"type": "integer", "default": 30},
            "author": {"type": "string"},
        },
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd: list[str] = [
            "log",
            "--no-merges",
            "--date=iso-strict",
            "--pretty=format:%H%x09%an%x09%ad%x09%s",
            f"-n{int(args.get('max_count') or 30)}",
        ]
        if args.get("since"):
            cmd.append(f"--since={args['since']}")
        if args.get("until"):
            cmd.append(f"--until={args['until']}")
        if args.get("author"):
            cmd.append(f"--author={args['author']}")
        if args.get("path"):
            cmd += ["--", args["path"]]
        out = _git(*cmd)
        commits = []
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                commits.append(
                    {"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
                )
        return {"commits": commits, "rows_count": len(commits)}


class GitShowTool(BaseTool):
    name = ToolName.GIT_SHOW
    description = (
        "Show the diff and metadata for a specific commit (or a single file in a commit)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sha": {"type": "string"},
            "path": {"type": "string", "description": "Optional file to scope the diff."},
            "max_lines": {"type": "integer", "default": 500},
        },
        "required": ["sha"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd = [
            "show",
            "--stat",
            "--patch",
            "--no-color",
            "--pretty=fuller",
            args["sha"],
        ]
        if args.get("path"):
            cmd += ["--", args["path"]]
        out = _git(*cmd)
        max_lines = int(args.get("max_lines") or 500)
        lines = out.splitlines()
        truncated = len(lines) > max_lines
        return {
            "sha": args["sha"],
            "diff": "\n".join(lines[:max_lines]),
            "truncated": truncated,
            "rows_count": min(max_lines, len(lines)),
        }


class GitBlameTool(BaseTool):
    name = ToolName.GIT_BLAME
    description = (
        "Blame a line range in a file. Useful to find when a particular selector / "
        "header / URL was last changed and by whom."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "required": ["path", "start_line", "end_line"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        rng = f"{int(args['start_line'])},{int(args['end_line'])}"
        out = _git("blame", "-L", rng, "--date=iso-strict", "--", args["path"])
        return {"path": args["path"], "blame": out, "rows_count": out.count("\n") + 1}
