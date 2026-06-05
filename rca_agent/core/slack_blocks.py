"""Format an RcaReport as Slack Block Kit blocks.

Designed to render the RCA as a readable summary in a single Slack message,
with the full evidence list reachable via the audit-log link (run_id).
"""
from __future__ import annotations

from typing import Any

from rca_agent.core.schemas import RcaReport


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _checklist_summary(report: RcaReport) -> str:
    if not report.investigation_checklist:
        return "_(no checks recorded)_"
    counts: dict[str, int] = {}
    for c in report.investigation_checklist:
        counts[c.status.value] = counts.get(c.status.value, 0) + 1
    parts = [f"*{counts[k]}* {k}" for k in sorted(counts)]
    return "  ·  ".join(parts)


def _chain_of_custody_text(report: RcaReport) -> str:
    if not report.chain_of_custody:
        return "_(not built)_"
    out = []
    for hop in report.chain_of_custody:
        marker = "✅" if hop.healthy else "❌"
        out.append(f"{marker} *{hop.layer}* — {_truncate(hop.observed, 120)}")
    return "\n".join(out)


def build_blocks(report: RcaReport, audit_url: str | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"RCA — {report.target_platform or '?'} / {report.target_module or '?'}",
            },
        }
    )

    if report.summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": report.summary}})

    top = report.top_hypothesis
    if top:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Root cause*\n{_truncate(top.title, 140)}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Confidence*\n{int(top.confidence * 100)}%  ({top.category})",
                    },
                ],
            }
        )
        if top.explanation:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _truncate(top.explanation, 2900)},
                }
            )

    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Chain of custody*\n" + _chain_of_custody_text(report)},
        }
    )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Investigation checklist:*  {_checklist_summary(report)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Tool calls:* {len(report.tool_calls)}  ·  *Evidence:* {len(report.evidence)}",
                },
            ],
        }
    )

    if report.fix and report.fix.kind.value != "none":
        fix_text = (
            f"*Proposed fix:* `{report.fix.kind.value}` ({int(report.fix.confidence * 100)}%)"
        )
        if report.pr_url:
            fix_text += f" — <{report.pr_url}|draft PR>"
        else:
            fix_text += " _(diff prepared, PR not opened)_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": fix_text}})

    ruled = report.ruled_out
    if ruled:
        bullets = "\n".join(f"• ~{_truncate(r.title, 110)}~" for r in ruled[:6])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Ruled out*\n" + bullets}})

    footer_parts = [f"run_id: `{report.run_id}`"]
    if audit_url:
        footer_parts.append(f"<{audit_url}|view full audit trail>")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  ·  ".join(footer_parts)}],
        }
    )

    return blocks


def fallback_text(report: RcaReport) -> str:
    top = report.top_hypothesis
    parts = [f"RCA for {report.target_platform or '?'}/{report.target_module or '?'}"]
    if report.summary:
        parts.append(report.summary)
    if top:
        parts.append(f"Root cause: {top.title} ({int(top.confidence * 100)}% conf)")
    return "  —  ".join(parts)
