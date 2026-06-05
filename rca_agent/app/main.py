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
from collections import defaultdict, deque
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from rca_agent.core.orchestrator import run_rca
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


def _kickoff_async(slack_url: str) -> str:
    """Start an RCA run in a background thread; return the run_id immediately."""
    audit = get_audit_logger()
    run_id_holder: dict[str, str] = {}
    ready = threading.Event()

    def _worker() -> None:
        try:
            for event in run_rca(slack_url):
                if not run_id_holder:
                    run_id_holder["id"] = event.run_id
                    ready.set()
                _emit(event.run_id, event.model_dump(mode="json"))
        except Exception as e:
            logger.exception("background run failed: {}", e)
            if run_id_holder:
                _emit(run_id_holder["id"], {"type": "error", "payload": {"error": str(e)}})

    threading.Thread(target=_worker, daemon=True).start()
    ready.wait(timeout=5.0)
    return run_id_holder.get("id", "")


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
async def stream(slack_url: str):
    if not slack_url.strip():
        raise HTTPException(400, "slack_url is required")
    run_id = _kickoff_async(slack_url.strip())
    if not run_id:
        raise HTTPException(500, "Could not start the run.")

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
