"""GitHub tools: list/get PRs, open a draft PR.

PR listing is the second half of git history: which commits actually shipped,
who reviewed them, what was the conversation. The agent often finds the
regression cause in a PR description, not the commit message.
"""
from __future__ import annotations

import subprocess
import textwrap
import uuid
from typing import Any

from github import Github, GithubException
from loguru import logger

from rca_agent.core.schemas import ToolName
from rca_agent.core.settings import settings
from rca_agent.tools.base import BaseTool, ToolError


def _client() -> Github:
    if not settings().GITHUB_TOKEN:
        raise ToolError("GITHUB_TOKEN is not set.")
    return Github(settings().GITHUB_TOKEN)


def _repo():
    return _client().get_repo(settings().GITHUB_REPO)


class GithubListPrsTool(BaseTool):
    name = ToolName.GITHUB_LIST_PRS
    description = (
        "List recent pull requests on the scraping repo, optionally filtered "
        "to ones touching a path. Use this after git.log to read the human "
        "context (description, reviews) for suspicious commits."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "all"},
            "path_contains": {
                "type": "string",
                "description": "Substring filter; only PRs whose changed files contain this are returned.",
            },
            "max_results": {"type": "integer", "default": 20},
        },
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = _repo()
        state = args.get("state") or "all"
        max_results = int(args.get("max_results") or 20)
        path_contains = (args.get("path_contains") or "").strip().lower()

        prs = repo.get_pulls(state=state, sort="updated", direction="desc")
        out: list[dict[str, Any]] = []
        scanned = 0
        for pr in prs:
            scanned += 1
            if scanned > 80:
                break
            if path_contains:
                files = [f.filename.lower() for f in pr.get_files()]
                if not any(path_contains in f for f in files):
                    continue
            out.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "merged": pr.merged,
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                    "user": pr.user.login if pr.user else None,
                    "url": pr.html_url,
                    "head_sha": pr.head.sha if pr.head else None,
                    "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                }
            )
            if len(out) >= max_results:
                break
        return {"prs": out, "rows_count": len(out)}


class GithubGetPrTool(BaseTool):
    name = ToolName.GITHUB_GET_PR
    description = "Get full details for a PR: description, changed files, review comments."
    input_schema = {
        "type": "object",
        "properties": {
            "number": {"type": "integer"},
        },
        "required": ["number"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = _repo()
        try:
            pr = repo.get_pull(int(args["number"]))
        except GithubException as e:
            raise ToolError(f"PR not found: {e.data}") from e
        files = []
        for f in pr.get_files():
            files.append(
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "patch": (f.patch or "")[:4000],
                }
            )
        return {
            "number": pr.number,
            "title": pr.title,
            "body": (pr.body or "")[:4000],
            "url": pr.html_url,
            "state": pr.state,
            "merged": pr.merged,
            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            "head_sha": pr.head.sha if pr.head else None,
            "base_ref": pr.base.ref if pr.base else None,
            "files": files,
            "rows_count": len(files),
        }


class GithubOpenPrTool(BaseTool):
    name = ToolName.GITHUB_OPEN_PR
    description = (
        "Open a DRAFT pull request on the scraping repo with the given diff. "
        "Use only for the four well-defined fix recipes (selector / header / "
        "pincode / URL pattern). The agent does NOT auto-merge; humans review."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "branch": {"type": "string", "description": "Branch name to create."},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "diff": {"type": "string", "description": "Unified diff to apply."},
            "base": {"type": "string", "default": "main"},
        },
        "required": ["branch", "title", "body", "diff"],
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        branch = args["branch"]
        base = args.get("base") or settings().GITHUB_BASE_BRANCH

        # Apply diff to a temporary branch using local git, then push.
        repo_root = str(settings().SCRAPING_REPO_ROOT)
        try:
            subprocess.run(["git", "-C", repo_root, "fetch", "origin", base], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", repo_root, "checkout", "-B", branch, f"origin/{base}"],
                check=True,
                capture_output=True,
            )
            apply = subprocess.run(
                ["git", "-C", repo_root, "apply", "--whitespace=fix", "-"],
                input=args["diff"],
                capture_output=True,
                text=True,
            )
            if apply.returncode != 0:
                raise ToolError(f"Could not apply diff: {apply.stderr.strip()[:500]}")
            subprocess.run(["git", "-C", repo_root, "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo_root,
                    "-c",
                    "user.email=rca-agent@gobblecube.local",
                    "-c",
                    "user.name=rca-agent",
                    "commit",
                    "-m",
                    args["title"],
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo_root, "push", "-u", "origin", branch],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise ToolError(
                f"git operation failed: {e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr}"
            ) from e

        try:
            pr = _repo().create_pull(
                title=args["title"],
                body=textwrap.dedent(args["body"]).strip(),
                head=branch,
                base=base,
                draft=True,
            )
        except GithubException as e:
            raise ToolError(f"create PR failed: {e.data}") from e

        return {"pr_url": pr.html_url, "number": pr.number, "branch": branch, "rows_count": 1}


def suggest_branch_name(prefix: str = "rca") -> str:
    return f"{prefix}/{uuid.uuid4().hex[:8]}"
