"""Single place where every tool the agent can call is registered."""
from __future__ import annotations

from typing import Any

from rca_agent.tools.base import BaseTool
from rca_agent.tools.clickup_tool import ClickupGetTicketTool
from rca_agent.tools.git_tool import GitBlameTool, GitLogTool, GitShowTool
from rca_agent.tools.github_tool import GithubGetPrTool, GithubListPrsTool
from rca_agent.tools.postgres_tool import PostgresDescribeTool, PostgresListTablesTool, PostgresQueryTool
from rca_agent.tools.registry_tool import RegistryResolveTool
from rca_agent.tools.repo_tool import RepoReadTool, RepoSearchTool
from rca_agent.tools.s3_tool import S3ListTool, S3ReadTool
from rca_agent.tools.slack_tool import SlackGetThreadTool
from rca_agent.tools.snowflake_tool import SnowflakeDescribeTool, SnowflakeListTablesTool, SnowflakeQueryTool
from rca_agent.tools.temporal_tool import TemporalHistoryTool


def build_investigation_tools() -> dict[str, BaseTool]:
    tools: list[BaseTool] = [
        SlackGetThreadTool(),
        ClickupGetTicketTool(),
        RegistryResolveTool(),
        SnowflakeListTablesTool(),
        SnowflakeDescribeTool(),
        SnowflakeQueryTool(),
        PostgresListTablesTool(),
        PostgresDescribeTool(),
        PostgresQueryTool(),
        RepoSearchTool(),
        RepoReadTool(),
        GitLogTool(),
        GitShowTool(),
        GitBlameTool(),
        GithubListPrsTool(),
        GithubGetPrTool(),
        S3ListTool(),
        S3ReadTool(),
        TemporalHistoryTool(),
    ]
    return {t.name.value: t for t in tools}


def anthropic_tool_specs(tools: dict[str, BaseTool], extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    specs = [t.to_anthropic_schema() for t in tools.values()]
    if extra:
        specs.extend(extra)
    return specs
