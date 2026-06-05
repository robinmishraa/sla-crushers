"""End-to-end RCA orchestrator.

Pipeline:
    parse Slack link → fetch thread → fetch ClickUp ticket
                    → extract Issue (LLM, structured)
                    → resolve target via temporal_v2 registry
                    → investigation loop (Claude tool-use)
                    → submit_rca (terminal tool) returns the structured report
                    → optionally open draft PR
                    → post RCA in the Slack thread

Every step yields a `StreamEvent` so the UI can render progress live.
The audit logger persists every tool call (and the final RCA) for replay.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from typing import Any, Iterator

from loguru import logger
from pydantic import ValidationError

from rca_agent.core.llm import call_messages, load_prompt
from rca_agent.core.schemas import (
    ChainOfCustodyHop,
    ChecklistItem,
    Evidence,
    FixProposal,
    FixProposalKind,
    Hypothesis,
    InvestigationPlanItem,
    Issue,
    NextAction,
    RcaReport,
    StreamEvent,
    VerdictKind,
)
from rca_agent.core.settings import settings
from rca_agent.core.slack_blocks import build_blocks, fallback_text
from rca_agent.core.tool_registry import anthropic_tool_specs, build_investigation_tools
from rca_agent.runs.logger import get_audit_logger
from rca_agent.tools.clickup_tool import ClickupGetTicketTool
from rca_agent.tools.github_tool import GithubOpenPrTool, suggest_branch_name
from rca_agent.tools.slack_tool import SlackGetThreadTool, SlackPostReplyTool, parse_slack_link


# ---------- Terminal tools ----------

_SUBMIT_RCA_SCHEMA: dict[str, Any] = {
    "name": "submit_rca",
    "description": (
        "Call this exactly once when your investigation is complete. "
        "Pass the full RCA report. After this call, no more tool calls will be made."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Primary executive summary (technical). If summary_technical is also provided, this can mirror it.",
            },
            "summary_business": {
                "type": "string",
                "description": "Plain-English summary for non-technical readers (1-2 sentences, no jargon).",
            },
            "summary_technical": {
                "type": "string",
                "description": "Technical summary for engineers (2-4 sentences, cites tool call ids and identifiers).",
            },
            "verdict_kind": {
                "type": "string",
                "enum": ["root_cause_found", "needs_ui_verification", "needs_deeper_probe", "inconclusive"],
                "description": "What kind of conclusion you reached — drives the UI banner.",
            },
            "next_actions": {
                "type": "array",
                "description": "Concrete things a human can/should do next. Include at least one unless verdict_kind=root_cause_found AND a fix PR was opened.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["human_verify_ui", "run_deep_probe", "review_pr", "manual_fix", "info"],
                        },
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "audience": {"type": "string", "enum": ["business", "technical", "both"]},
                        "suggested_tool": {
                            "type": "string",
                            "description": "If kind=run_deep_probe: the tool name (e.g. 's3.read', 'snowflake.query').",
                        },
                        "suggested_args": {
                            "type": "object",
                            "description": "Pre-baked args for the suggested tool.",
                        },
                    },
                    "required": ["kind", "title"],
                },
            },
            "investigation_plan": {
                "type": "array",
                "description": "Your triage decisions: which layers you chose to check and which you skipped (and why).",
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": ["registry", "snowflake", "postgres", "s3", "temporal",
                                     "spider_code", "git_history", "github_prs", "other"],
                        },
                        "description": {"type": "string"},
                        "will_check": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["layer", "description"],
                },
            },
            "target_platform": {"type": "string"},
            "target_module": {"type": "string"},
            "target_runner": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "summary": {"type": "string"},
                        "tool_call_id": {"type": "string"},
                        "location": {"type": "string"},
                        "detail": {"type": "object"},
                    },
                    "required": ["id", "kind", "summary", "tool_call_id"],
                },
            },
            "chain_of_custody": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {
                            "type": "string",
                            "enum": ["temporal_run", "s3_raw", "postgres", "snowflake", "report"],
                        },
                        "expected": {"type": "string"},
                        "observed": {"type": "string"},
                        "healthy": {"type": "boolean"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["layer", "expected", "observed", "healthy"],
                },
            },
            "investigation_checklist": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["passed", "failed", "inconclusive", "skipped", "not_run"],
                        },
                        "finding": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["key", "label", "status"],
                },
            },
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "explanation": {"type": "string"},
                        "confidence": {"type": "number"},
                        "status": {
                            "type": "string",
                            "enum": ["supported", "ruled_out", "unverified"],
                        },
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "rule_out_reason": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["title", "explanation", "confidence", "status"],
                },
            },
            "fix": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "selector_update",
                            "header_update",
                            "pincode_update",
                            "url_pattern_update",
                            "none",
                        ],
                    },
                    "rationale": {"type": "string"},
                    "file_path": {"type": "string"},
                    "diff": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind"],
            },
        },
        "required": ["summary", "evidence", "hypotheses", "investigation_checklist", "chain_of_custody"],
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _evt(event_type: str, run_id: str, **payload: Any) -> StreamEvent:
    return StreamEvent(type=event_type, run_id=run_id, timestamp=_now(), payload=payload)


def _truncate_for_llm(obj: Any, limit: int = 12_000) -> Any:
    """Trim large tool results before feeding back to the LLM."""
    text = json.dumps(obj, default=str)
    if len(text) <= limit:
        return obj
    return {
        "_truncated": True,
        "_original_bytes": len(text),
        "preview": text[:limit] + "... [truncated]",
    }


# ---------- Issue extraction ----------

def _extract_issue(thread: dict[str, Any], ticket: dict[str, Any] | None, slack_url: str) -> Issue:
    """Ask the LLM to produce an Issue from the thread + ticket (structured tool use)."""
    schema = {
        "name": "submit_issue",
        "description": "Submit the structured Issue extracted from the thread + ticket.",
        "input_schema": Issue.model_json_schema(),
    }
    parsed = parse_slack_link(slack_url)

    user_blob = {
        "slack_link": slack_url,
        "slack_channel": parsed.channel,
        "slack_parent_ts": parsed.parent_ts,
        "thread": thread,
        "clickup_ticket": ticket,
    }
    sys = load_prompt("extract_issue")
    resp = call_messages(
        system=sys,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract a structured Issue. Call submit_issue with the result. "
                            "Inputs (JSON):\n```json\n"
                            + json.dumps(user_blob, default=str)[:18_000]
                            + "\n```"
                        ),
                    }
                ],
            }
        ],
        tools=[schema],
        max_tokens=2048,
        temperature=0.0,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_issue":
            try:
                issue = Issue(**block.input)
            except ValidationError:
                # If the LLM returned partial fields, fill what we can.
                issue = Issue(**{k: v for k, v in block.input.items() if v is not None})
            issue.ticket_url = (ticket or {}).get("url") or issue.ticket_url
            issue.slack_channel = parsed.channel
            issue.slack_ts = parsed.ts
            issue.slack_thread_ts = parsed.parent_ts
            return issue
    # Fallback: minimal issue
    raw = " ".join(m.get("text", "") for m in thread.get("messages", []))[:280]
    return Issue(
        raw_summary=raw or "Could not extract issue automatically.",
        ticket_url=(ticket or {}).get("url"),
        slack_channel=parsed.channel,
        slack_ts=parsed.ts,
        slack_thread_ts=parsed.parent_ts,
    )


# ---------- Main orchestrator ----------

def run_rca(slack_url: str) -> Iterator[StreamEvent]:
    audit = get_audit_logger()
    run_id = audit.start_run(slack_url)
    started_at = _now()

    try:
        yield _evt("status", run_id, message="Fetching Slack thread")
        thread_result = SlackGetThreadTool().run({"url": slack_url}, run_id=run_id)
        if "error" in thread_result:
            yield _evt("error", run_id, error=thread_result["error"])
            audit.finish_run(run_id, "failed")
            return

        clickup_urls = thread_result.get("clickup_urls", [])
        ticket_result: dict[str, Any] | None = None
        if clickup_urls:
            yield _evt("status", run_id, message=f"Fetching ClickUp ticket {clickup_urls[0]}")
            ticket_result = ClickupGetTicketTool().run(
                {"url_or_id": clickup_urls[0]}, run_id=run_id
            )
            if "error" in ticket_result:
                logger.warning("ClickUp fetch failed: {}", ticket_result["error"])
                ticket_result = None

        yield _evt("status", run_id, message="Extracting structured issue")
        issue = _extract_issue(thread_result, ticket_result, slack_url)
        yield _evt("issue_extracted", run_id, issue=issue.model_dump(mode="json"))

        # Build tool registry for the investigation loop
        tools = build_investigation_tools()
        tool_specs = anthropic_tool_specs(tools, extra=[_SUBMIT_RCA_SCHEMA])

        system_prompt = load_prompt("system")
        compose_prompt = load_prompt("compose_rca")
        fix_prompt = load_prompt("propose_fix")
        full_system = "\n\n---\n\n".join([system_prompt, compose_prompt, fix_prompt])

        bootstrap_user = {
            "slack_url": slack_url,
            "extracted_issue": issue.model_dump(mode="json"),
            "slack_thread": thread_result,
            "clickup_ticket": ticket_result,
        }

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Begin the RCA investigation. Bootstrap context (already fetched, "
                            "do NOT re-fetch unless a tool result is stale):\n```json\n"
                            + json.dumps(bootstrap_user, default=str)[:18_000]
                            + "\n```\n\nFollow the playbook. End by calling submit_rca."
                        ),
                    }
                ],
            }
        ]

        rca_payload: dict[str, Any] | None = None
        budget_left = settings().TOOL_BUDGET

        while budget_left > 0 and rca_payload is None:
            yield _evt("status", run_id, message=f"Reasoning… (tool budget: {budget_left})")
            resp = call_messages(
                system=full_system,
                messages=messages,
                tools=tool_specs,
                max_tokens=8192,
                temperature=0.1,
            )

            assistant_blocks: list[dict[str, Any]] = []
            tool_uses: list[Any] = []
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    assistant_blocks.append({"type": "text", "text": block.text})
                elif btype == "tool_use":
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                    tool_uses.append(block)

            messages.append({"role": "assistant", "content": assistant_blocks})

            if not tool_uses:
                # No tool calls and no submit_rca — bail out cleanly.
                yield _evt("status", run_id, message="LLM ended without submitting RCA; stopping.")
                break

            tool_results_content: list[dict[str, Any]] = []
            for tu in tool_uses:
                if tu.name == "submit_rca":
                    rca_payload = tu.input
                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": "RCA accepted.",
                        }
                    )
                    continue

                tool = tools.get(tu.name)
                yield _evt(
                    "tool_call_started",
                    run_id,
                    tool=tu.name,
                    args=tu.input,
                    tool_use_id=tu.id,
                )
                if not tool:
                    result: dict[str, Any] = {"error": f"Unknown tool: {tu.name}"}
                else:
                    result = tool.run(tu.input or {}, run_id=run_id)
                budget_left -= 1
                yield _evt(
                    "tool_call_finished",
                    run_id,
                    tool=tu.name,
                    ok="error" not in result,
                    rows=result.get("rows_count"),
                    tool_call_id=result.get("_tool_call_id"),
                )

                tool_results_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(_truncate_for_llm(result), default=str),
                        "is_error": "error" in result,
                    }
                )

            messages.append({"role": "user", "content": tool_results_content})

            if rca_payload is not None:
                break

        if rca_payload is None:
            err = "Tool budget exhausted before RCA was submitted."
            yield _evt("error", run_id, error=err)
            audit.finish_run(run_id, "failed")
            return

        try:
            evidence = [Evidence(**e) for e in rca_payload.get("evidence", [])]
            hypotheses = [Hypothesis(**h) for h in rca_payload.get("hypotheses", [])]
            checklist = [ChecklistItem(**c) for c in rca_payload.get("investigation_checklist", [])]
            chain = [ChainOfCustodyHop(**h) for h in rca_payload.get("chain_of_custody", [])]
            fix = FixProposal(**rca_payload.get("fix") or {})
            next_actions = [NextAction(**a) for a in rca_payload.get("next_actions", [])]
            plan = [InvestigationPlanItem(**p) for p in rca_payload.get("investigation_plan", [])]
        except ValidationError as e:
            yield _evt("error", run_id, error=f"submit_rca payload invalid: {e}")
            audit.finish_run(run_id, "failed")
            return

        # Verdict — fall back to inconclusive if the LLM forgets it
        verdict_raw = rca_payload.get("verdict_kind") or VerdictKind.INCONCLUSIVE.value
        try:
            verdict = VerdictKind(verdict_raw)
        except ValueError:
            verdict = VerdictKind.INCONCLUSIVE

        # Make sure both summary fields end up populated (back-compat: `summary` mirrors technical)
        summary_tech = rca_payload.get("summary_technical") or rca_payload.get("summary") or ""
        summary_biz = rca_payload.get("summary_business") or ""
        summary = rca_payload.get("summary") or summary_tech

        report = RcaReport(
            issue=issue,
            target_platform=rca_payload.get("target_platform") or issue.platform,
            target_module=rca_payload.get("target_module") or issue.module,
            target_runner=rca_payload.get("target_runner"),
            summary=summary,
            summary_technical=summary_tech,
            summary_business=summary_biz,
            verdict_kind=verdict,
            next_actions=next_actions,
            investigation_plan=plan,
            evidence=evidence,
            chain_of_custody=chain,
            investigation_checklist=checklist,
            hypotheses=hypotheses,
            fix=fix,
            started_at=started_at,
            finished_at=_now(),
            run_id=run_id,
        )

        # ---- optional: open a draft PR if confidence threshold met and we have a diff ----
        if (
            settings().GITHUB_TOKEN
            and report.fix.kind != FixProposalKind.NONE
            and report.fix.diff
            and report.fix.confidence >= settings().AUTO_PR_MIN_CONFIDENCE
        ):
            yield _evt("status", run_id, message="Opening draft PR")
            top = report.top_hypothesis
            pr_body = (
                f"_Auto-generated by sla-crushers (run `{run_id}`)._\n\n"
                f"## Root cause\n"
                f"{top.title if top else '(no top hypothesis)'} "
                f"(confidence {int((top.confidence if top else 0) * 100)}%)\n\n"
                f"## Why this fix\n{report.fix.rationale}\n\n"
                f"## RCA summary\n{report.summary}\n"
            )
            pr_args = {
                "branch": suggest_branch_name(prefix=f"rca-{report.target_platform or 'fix'}"),
                "title": f"[rca] {report.fix.kind.value}: {report.summary[:80]}",
                "body": pr_body,
                "diff": report.fix.diff,
                "base": settings().GITHUB_BASE_BRANCH,
            }
            pr_result = GithubOpenPrTool().run(pr_args, run_id=run_id)
            if "error" not in pr_result:
                report.pr_url = pr_result.get("pr_url")
                yield _evt("pr_opened", run_id, pr_url=report.pr_url, number=pr_result.get("number"))
            else:
                yield _evt("status", run_id, message=f"Could not open PR: {pr_result.get('error')}")

        # ---- post the RCA back into the Slack thread ----
        if (
            settings().SLACK_BOT_TOKEN
            and issue.slack_channel
            and issue.slack_thread_ts
        ):
            yield _evt("status", run_id, message="Posting RCA to Slack thread")
            try:
                blocks = build_blocks(report)
                SlackPostReplyTool().run(
                    {
                        "channel": issue.slack_channel,
                        "thread_ts": issue.slack_thread_ts,
                        "text": fallback_text(report),
                        "blocks": blocks,
                    },
                    run_id=run_id,
                )
            except Exception as e:
                logger.warning("Slack post failed: {}", e)

        yield _evt("rca_complete", run_id, rca=report.model_dump(mode="json"))
        audit.finish_run(run_id, "ok", rca=report.model_dump(mode="json"))

    except Exception as e:
        logger.exception("orchestrator failed")
        yield _evt("error", run_id, error=str(e), traceback=traceback.format_exc())
        audit.finish_run(run_id, "failed")
