"""FastAPI app: paste a Slack link, watch the agent work, see the final RCA.

Endpoints:
  GET  /                    → single-page UI
  POST /rca                 → start a run, return run_id (sync) — used for slash command etc.
  GET  /rca/stream?url=...  → start a run AND stream events as Server-Sent Events
  GET  /runs                → list past runs (audit)
  GET  /runs/{run_id}       → drill into one run (every tool call)
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from rca_agent.core.orchestrator import run_rca
from rca_agent.core.tool_registry import build_investigation_tools
from rca_agent.runs.logger import get_audit_logger


app = FastAPI(title="sla-crushers", description="Slack link → RCA")

# ---- in-memory event broker so SSE clients can attach to a running job ----
_run_queues: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
_run_done: dict[str, bool] = {}
_run_lock = threading.Lock()


def _emit(run_id: str, event: dict[str, Any]) -> None:
    with _run_lock:
        _run_queues[run_id].append(event)
        if event.get("type") in {"rca_complete", "error"}:
            _run_done[run_id] = True


def _kickoff_async(
    slack_url: str,
    thread_text: str | None = None,
    clickup_url: str | None = None,
) -> str:
    """Start an RCA run in a background thread; return the run_id immediately.

    If the run fails before any event is emitted (e.g. missing SCRAPING_REPO_ROOT,
    bad Anthropic key), we mint a synthetic run_id and emit a single error event
    so the SSE stream can render a clean error in the UI instead of returning 500.
    """
    run_id_holder: dict[str, str] = {}
    ready = threading.Event()

    def _emit_synthetic_error(message: str, where: str) -> str:
        rid = run_id_holder.get("id") or f"err-{uuid.uuid4().hex[:8]}"
        run_id_holder.setdefault("id", rid)
        _emit(
            rid,
            {
                "type": "error",
                "run_id": rid,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"error": message, "where": where},
            },
        )
        ready.set()
        return rid

    def _worker() -> None:
        try:
            for event in run_rca(slack_url, thread_text=thread_text, clickup_url=clickup_url):
                if not run_id_holder:
                    run_id_holder["id"] = event.run_id
                    ready.set()
                _emit(event.run_id, event.model_dump(mode="json"))
        except Exception as e:
            logger.exception("background run failed: {}", e)
            _emit_synthetic_error(str(e), where="orchestrator")

    threading.Thread(target=_worker, daemon=True).start()
    ready.wait(timeout=5.0)
    return run_id_holder.get("id", "") or _emit_synthetic_error(
        "Run failed to start within 5s (check server logs).",
        where="kickoff_timeout",
    )


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "ui" / "index.html")


@app.post("/rca")
def kickoff(payload: dict[str, str]) -> dict[str, str]:
    url = (payload or {}).get("slack_url", "").strip()
    if not url:
        raise HTTPException(400, "slack_url is required")
    run_id = _kickoff_async(url)
    if not run_id:
        raise HTTPException(500, "Could not start the run.")
    return {"run_id": run_id}


@app.get("/rca/stream")
async def stream(slack_url: str, thread_text: str | None = None, clickup_url: str | None = None):
    if not slack_url.strip():
        raise HTTPException(400, "slack_url is required")
    try:
        run_id = _kickoff_async(
            slack_url.strip(),
            thread_text=(thread_text or "").strip() or None,
            clickup_url=(clickup_url or "").strip() or None,
        )
    except Exception as e:
        logger.exception("kickoff failed before worker started")
        rid = f"err-{uuid.uuid4().hex[:8]}"
        err_event = {
            "type": "error",
            "run_id": rid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"error": str(e), "where": "kickoff"},
        }

        async def err_gen():
            yield {"event": "error", "data": json.dumps(err_event, default=str)}
            yield {"event": "end", "data": json.dumps({"run_id": rid})}

        return EventSourceResponse(err_gen())

    async def gen():
        cursor = 0
        while True:
            with _run_lock:
                events = list(_run_queues[run_id])
            new_events = events[cursor:]
            cursor = len(events)
            for ev in new_events:
                yield {"event": ev.get("type", "message"), "data": json.dumps(ev, default=str)}
            with _run_lock:
                done = _run_done.get(run_id, False)
            if done and cursor == len(events):
                yield {"event": "end", "data": json.dumps({"run_id": run_id})}
                return
            import asyncio
            await asyncio.sleep(0.4)

    return EventSourceResponse(gen())


@app.get("/runs")
def list_runs(limit: int = 50) -> JSONResponse:
    return JSONResponse(get_audit_logger().list_runs(limit=limit))


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    data = get_audit_logger().get_run(run_id)
    if not data:
        raise HTTPException(404, "run not found")
    return JSONResponse(data)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True}


# Cache of tool instances, built lazily — the registry can import the scraping repo,
# so we don't want that on FastAPI startup.
_PROBE_TOOLS: dict[str, Any] | None = None


def _get_probe_tools() -> dict[str, Any]:
    global _PROBE_TOOLS
    if _PROBE_TOOLS is None:
        _PROBE_TOOLS = build_investigation_tools()
    return _PROBE_TOOLS


@app.post("/rca/probe")
def probe(payload: dict[str, Any]) -> JSONResponse:
    """Run a single tool against an existing run, on-demand from the UI.

    Powers the "Run this probe" button on next_action cards: the LLM proposes the tool +
    args, the human reviews and approves, the UI hits this endpoint, the result lands in
    the same run's audit log so it's reproducible.
    """
    run_id = (payload or {}).get("run_id", "").strip()
    tool_name = (payload or {}).get("tool", "").strip()
    args = (payload or {}).get("args") or {}
    if not run_id:
        raise HTTPException(400, "run_id is required")
    if not tool_name:
        raise HTTPException(400, "tool is required")

    tools = _get_probe_tools()
    tool = tools.get(tool_name)
    if tool is None:
        raise HTTPException(404, f"Unknown tool: {tool_name}. Known: {sorted(tools.keys())}")

    try:
        result = tool.run(args, run_id=run_id)
    except Exception as e:
        logger.exception("probe failed")
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": str(e), "tool": tool_name, "args": args},
        )

    ok = "error" not in result
    return JSONResponse(
        {"ok": ok, "tool": tool_name, "args": args, "result": result}
    )


@app.get("/preflight")
def preflight() -> JSONResponse:
    """Check the live config so the UI can warn the user before they hit Run."""
    issues: list[dict[str, str]] = []
    info: dict[str, Any] = {}
    try:
        from rca_agent.core.settings import settings as _s
        s = _s()
        info["scraping_repo_root"] = str(s.SCRAPING_REPO_ROOT) if s.SCRAPING_REPO_ROOT else None
        info["llm_model"] = s.LLM_MODEL
        info["llm_provider"] = s.LLM_PROVIDER

        # SCRAPING_REPO_ROOT
        if s.SCRAPING_REPO_ROOT is None:
            raw = s.SCRAPING_REPO_ROOT_RAW
            if raw:
                msg = (
                    f"SCRAPING_REPO_ROOT={raw!r} does not exist on this machine. "
                    "Edit .env to point at your local clone of GobbleCube/scraping."
                )
            else:
                msg = "SCRAPING_REPO_ROOT is not set in .env."
            issues.append({"severity": "error", "key": "SCRAPING_REPO_ROOT", "message": msg})

        # Required secrets
        for key, label in [
            ("ANTHROPIC_API_KEY", "Anthropic API key"),
            ("SLACK_BOT_TOKEN", "Slack bot token"),
        ]:
            val = getattr(s, key, None)
            if not val or str(val).startswith(("xoxb-...", "sk-ant-...")) or val.endswith("..."):
                issues.append({"severity": "error", "key": key, "message": f"{label} is not set in .env"})

        # Optional secrets
        if not s.CLICKUP_API_TOKEN or s.CLICKUP_API_TOKEN.endswith("..."):
            issues.append(
                {"severity": "warn", "key": "CLICKUP_API_TOKEN",
                 "message": "ClickUp token is not set — ticket lookups will be skipped."}
            )
        if not s.GITHUB_TOKEN or s.GITHUB_TOKEN.endswith("..."):
            issues.append(
                {"severity": "warn", "key": "GITHUB_TOKEN",
                 "message": "GitHub token is not set — PR listing + auto-PR will be disabled."}
            )
    except Exception as e:
        issues.append({"severity": "error", "key": "settings", "message": str(e)})
    return JSONResponse({"ok": not any(i["severity"] == "error" for i in issues),
                         "issues": issues, "info": info})
