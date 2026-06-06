"""Pydantic schemas for the RCA agent.

These are the single source of truth for the contract between
the LLM, the orchestrator, the tools, and the UI.

Design notes:
- Every Hypothesis MUST cite Evidence ids — no hand-wavy RCAs.
- Every Evidence row carries the exact tool call that produced it,
  so a reviewer can re-run it from the audit log.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------- Issue extraction ----------

class IssueIntent(str, Enum):
    MISSING_DATA = "missing_data"          # rows expected, none/few present
    STALE_DATA = "stale_data"              # last update too old
    WRONG_VALUE = "wrong_value"            # price/title/stock looks wrong
    EMPTY_RESPONSE = "empty_response"      # spider scraping but yielding 0
    BLOCKED = "blocked"                    # 403/429/captcha/WAF
    SCHEMA_MISMATCH = "schema_mismatch"    # field renamed/removed upstream
    LOCATION_MISMATCH = "location_mismatch"  # wrong pincode/pluscode
    PARTIAL_FAILURE = "partial_failure"    # some items succeed, some don't
    OTHER = "other"


class TimeWindow(BaseModel):
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    natural: Optional[str] = Field(
        default=None,
        description="Original natural-language phrase (e.g. 'yesterday', 'last 3 hrs').",
    )


class Issue(BaseModel):
    """Structured representation of what the user is complaining about."""
    raw_summary: str = Field(..., description="One-line human description.")
    platform: Optional[str] = Field(default=None, description="Maps to Platform enum.")
    module: Optional[str] = Field(default=None, description="search | pdp | brand | product_listing")
    country: Optional[str] = Field(default=None, description="ISO-2 if known (IN, AE, SA, MX, TR, BH).")
    city_or_pincode: Optional[str] = None
    identifiers: list[str] = Field(
        default_factory=list,
        description="ASINs / keywords / product slugs / SKU ids mentioned in the ticket.",
    )
    intent: IssueIntent = IssueIntent.OTHER
    time_window: TimeWindow = Field(default_factory=TimeWindow)
    ticket_url: Optional[str] = None
    slack_channel: Optional[str] = None
    slack_ts: Optional[str] = None
    slack_thread_ts: Optional[str] = None


# ---------- Tool calls + Evidence ----------

class ToolName(str, Enum):
    SLACK_GET_THREAD = "slack.get_thread"
    SLACK_POST_REPLY = "slack.post_reply"
    CLICKUP_GET_TICKET = "clickup.get_ticket"
    SF_QUERY = "snowflake.query"
    SF_LIST_TABLES = "snowflake.list_tables"
    SF_DESCRIBE = "snowflake.describe"
    PG_QUERY = "postgres.query"
    PG_LIST_TABLES = "postgres.list_tables"
    PG_DESCRIBE = "postgres.describe"
    REPO_SEARCH = "repo.search"
    REPO_READ = "repo.read"
    REGISTRY_RESOLVE = "registry.resolve"
    TEMPORAL_HISTORY = "temporal.history"
    S3_LIST = "s3.list"
    S3_READ = "s3.read"
    GIT_LOG = "git.log"
    GIT_SHOW = "git.show"
    GIT_BLAME = "git.blame"
    GITHUB_LIST_PRS = "github.list_prs"
    GITHUB_GET_PR = "github.get_pr"
    GITHUB_OPEN_PR = "github.open_pr"


class ToolCall(BaseModel):
    """A single recorded tool invocation."""
    id: str
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: Optional[datetime] = None
    ok: bool = True
    error: Optional[str] = None
    result_preview: Optional[str] = Field(
        default=None,
        description="First ~2KB of the result, for the audit trail.",
    )
    rows: Optional[int] = Field(default=None, description="Row count for SQL tools.")


# Canonical Evidence kinds — listed for the LLM's reference, but the schema
# accepts any string so a slightly-off label (e.g. "code_inspection") doesn't
# tank the entire run at final validation time.
CANONICAL_EVIDENCE_KINDS = (
    "sql_result", "code_reference", "registry_entry", "slack_message",
    "clickup_ticket", "temporal_history", "s3_artifact", "git_commit",
    "github_pr", "code_inspection", "tool_unavailable", "other",
)


class Evidence(BaseModel):
    """A single fact the agent collected, anchored to a tool call."""
    id: str = Field(..., description="Stable id for hypothesis citations, e.g. 'E1'.")
    kind: str = Field(
        ...,
        description=(
            "Category for grouping. Preferred values: " + ", ".join(CANONICAL_EVIDENCE_KINDS)
            + ". Free-form strings are accepted."
        ),
    )
    summary: str = Field(..., description="One-line human-readable claim.")
    tool_call_id: str = Field(..., description="ID of the ToolCall that produced this.")
    detail: dict[str, Any] = Field(default_factory=dict)
    location: Optional[str] = Field(
        default=None,
        description="path:line for code, table for sql, url for slack/clickup, etc.",
    )


# ---------- Hypothesis + RCA ----------

class HypothesisStatus(str, Enum):
    SUPPORTED = "supported"
    RULED_OUT = "ruled_out"
    UNVERIFIED = "unverified"


# Canonical Hypothesis categories — listed for the LLM's reference, but the
# schema accepts any string so a slightly-off label (e.g. "platform_ui_issue")
# doesn't tank the entire run at final validation time.
CANONICAL_HYPOTHESIS_CATEGORIES = (
    "selector_drift", "header_or_cookie", "pincode_or_location", "url_pattern",
    "upstream_schema_change", "rate_limit_or_block", "infra_or_pipeline",
    "data_pipeline_lag", "input_data_issue", "code_regression", "config_change",
    "platform_ui_issue", "other",
)


class Hypothesis(BaseModel):
    title: str
    explanation: str = Field(..., description="Plain English root cause.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    status: str = Field(
        default="unverified",
        description="One of: supported, ruled_out, unverified. Other strings are accepted.",
    )
    evidence_ids: list[str] = Field(default_factory=list)
    rule_out_reason: Optional[str] = Field(
        default=None,
        description="Required when status == RULED_OUT; explains which evidence killed it.",
    )
    category: str = Field(
        default="other",
        description=(
            "Hypothesis category. Preferred values: "
            + ", ".join(CANONICAL_HYPOTHESIS_CATEGORIES)
            + ". Free-form strings are accepted."
        ),
    )


class ChecklistStatus(str, Enum):
    PASSED = "passed"        # check ran, result was healthy
    FAILED = "failed"        # check ran, result was unhealthy (this is a positive finding)
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"      # not applicable to this issue type
    NOT_RUN = "not_run"


class ChecklistItem(BaseModel):
    """One concrete thing the agent verified.

    Lean is fine — three sharp checks beat fifteen filler ones.
    """
    key: str = Field(..., description="Stable id, e.g. 'sf_recent_rows'.")
    label: str = Field(..., description="Human-readable name of the check.")
    status: str = Field(
        default="not_run",
        description="One of: passed, failed, inconclusive, skipped, not_run. Free-form strings are accepted.",
    )
    finding: str = Field(default="", description="One-line result of the check.")
    evidence_ids: list[str] = Field(default_factory=list)


CANONICAL_CHAIN_LAYERS = (
    "temporal_run", "s3_raw", "postgres", "snowflake", "report",
    "spider_code", "platform_ui", "other",
)


class ChainOfCustodyHop(BaseModel):
    """Tracks data through the pipeline: spider -> S3 -> Postgres -> Snowflake."""
    layer: str = Field(
        ...,
        description="Pipeline layer label. Preferred: " + ", ".join(CANONICAL_CHAIN_LAYERS),
    )
    expected: str = Field(..., description="What we expected at this layer.")
    observed: str = Field(..., description="What we actually saw.")
    healthy: bool
    evidence_ids: list[str] = Field(default_factory=list)


class FixProposalKind(str, Enum):
    SELECTOR_UPDATE = "selector_update"
    HEADER_UPDATE = "header_update"
    PINCODE_UPDATE = "pincode_update"
    URL_PATTERN_UPDATE = "url_pattern_update"
    NONE = "none"


class FixProposal(BaseModel):
    kind: FixProposalKind = FixProposalKind.NONE
    rationale: str = ""
    file_path: Optional[str] = None
    diff: Optional[str] = Field(
        default=None,
        description="Unified diff that the recipe will apply. None if NONE.",
    )
    confidence: float = 0.0


# ---------- Verdict + next-action machinery ----------

class VerdictKind(str, Enum):
    ROOT_CAUSE_FOUND = "root_cause_found"
    """Confident root cause supported by ≥2 evidence items."""

    NEEDS_UI_VERIFICATION = "needs_ui_verification"
    """Data + code layers look healthy; the symptom is most likely downstream of the scraping
    pipeline. Ask the human to check the platform UI before going deeper."""

    NEEDS_DEEPER_PROBE = "needs_deeper_probe"
    """Standard checks are inconclusive. The agent proposes one or more opt-in deep probes
    (e.g. re-scrape a single URL, pull recent S3 artifacts) for the human to run."""

    INCONCLUSIVE = "inconclusive"
    """Genuinely unknown; budget exhausted with no useful next probe."""


class NextActionKind(str, Enum):
    HUMAN_VERIFY_UI = "human_verify_ui"
    """The human should open the platform dashboard and confirm what they see."""

    RUN_DEEP_PROBE = "run_deep_probe"
    """A pre-baked tool call the human can opt to run from the UI."""

    REVIEW_PR = "review_pr"
    """Review the auto-opened draft PR."""

    MANUAL_FIX = "manual_fix"
    """A fix that doesn't fall into the 4 recipe kinds — copy/paste instructions."""

    INFO = "info"
    """Informational note, no action required."""


class NextAction(BaseModel):
    """Something a human can / should do after reading the RCA.

    Some are pre-baked tool calls the UI can fire via /rca/probe (`suggested_tool` + `suggested_args`).
    Others are pure instructions (`detail`).
    """
    kind: NextActionKind
    title: str = Field(..., description="Short label, e.g. 'Verify on Blinkit dashboard'.")
    detail: str = Field("", description="Longer human-readable instruction.")
    audience: Literal["business", "technical", "both"] = "both"
    suggested_tool: Optional[str] = Field(
        default=None,
        description="If set, the UI shows a 'Run this probe' button that calls this tool.",
    )
    suggested_args: Optional[dict[str, Any]] = Field(
        default=None,
        description="Pre-baked args to send to the tool when the human approves.",
    )


CANONICAL_PLAN_LAYERS = (
    "registry", "snowflake", "postgres", "s3", "temporal",
    "spider_code", "git_history", "github_prs", "platform_ui", "other",
)


class InvestigationPlanItem(BaseModel):
    """One thing the agent intends to (or did) check, with its rationale."""
    layer: str = Field(
        ...,
        description="Pipeline layer. Preferred: " + ", ".join(CANONICAL_PLAN_LAYERS),
    )
    description: str
    will_check: bool = Field(
        default=True,
        description="False if the agent decided this layer is irrelevant to the symptom.",
    )
    reason: str = Field(default="", description="Why the agent included or skipped this layer.")


class RcaReport(BaseModel):
    issue: Issue
    target_platform: Optional[str] = None
    target_module: Optional[str] = None
    target_runner: Optional[str] = Field(
        default=None, description="Module path of the runner config (e.g. runners.blinkit.search)."
    )

    # ---- Dual-audience summaries ----
    summary: str = Field(
        ...,
        description=(
            "Primary executive summary (technical). Kept for backwards compatibility — "
            "mirrors summary_technical if set."
        ),
    )
    summary_business: str = Field(
        default="",
        description=(
            "Plain-English summary for non-technical users (PM / ops / business). "
            "No table names, no SQL, no file paths. One or two sentences."
        ),
    )
    summary_technical: str = Field(
        default="",
        description=(
            "Technical summary for engineers. 2-4 sentences citing specific tables, columns, "
            "queries, file:line refs, and PR numbers."
        ),
    )

    # ---- Verdict & action machinery ----
    verdict_kind: VerdictKind = Field(
        default=VerdictKind.INCONCLUSIVE,
        description="What kind of conclusion the agent reached — drives the UI banner.",
    )
    next_actions: list[NextAction] = Field(
        default_factory=list,
        description="Concrete things a human can / should do next. Some are runnable deep probes.",
    )
    investigation_plan: list[InvestigationPlanItem] = Field(
        default_factory=list,
        description="The triage plan the agent set up before / during the investigation. "
                    "Used by the UI to explain *why* certain layers were skipped.",
    )

    # ---- Findings ----
    evidence: list[Evidence] = Field(default_factory=list)
    chain_of_custody: list[ChainOfCustodyHop] = Field(
        default_factory=list,
        description="End-to-end pipeline walk: temporal -> s3 -> postgres -> snowflake -> report.",
    )
    investigation_checklist: list[ChecklistItem] = Field(
        default_factory=list,
        description="Every concrete check the agent performed. This is the 'I checked everything' artifact.",
    )
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        description="ALL hypotheses considered. Status field marks supported vs ruled_out.",
    )
    fix: FixProposal = Field(default_factory=FixProposal)
    pr_url: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    started_at: datetime
    finished_at: Optional[datetime] = None
    run_id: str

    @property
    def top_hypothesis(self) -> Optional[Hypothesis]:
        supported = [h for h in self.hypotheses if h.status == HypothesisStatus.SUPPORTED]
        if not supported:
            return None
        return max(supported, key=lambda h: h.confidence)

    @property
    def ruled_out(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.RULED_OUT]


# ---------- Streaming events for the UI ----------

class StreamEvent(BaseModel):
    """Server-sent event payload."""
    type: Literal[
        "status",
        "issue_extracted",
        "tool_call_started",
        "tool_call_finished",
        "evidence_added",
        "hypothesis",
        "fix_proposed",
        "pr_opened",
        "rca_complete",
        "error",
    ]
    run_id: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
