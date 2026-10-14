"""PRGuard server: GitHub webhook receiver, API and the Mission Control dashboard.

Run:  prguard serve            (or: uvicorn backend.main:app --host 0.0.0.0 --port 8000)

Webhook (GitHub App or repo webhook, content type application/json, secret =
GITHUB_WEBHOOK_SECRET) with events "Pull requests" and "Issue comments".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from typing import Any

import hmac

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import commands, triggers
from backend.agents import aggregator
from backend.config import settings
from backend.context import Options, load_config
from backend.events import bus
from backend.gateway import gateway, mock_upstream
from backend.models import CommandResult, Denial, Event, TaskContext, UpstreamError
from backend.repo_config import COMMANDS, RepoConfig
from backend.sandbox.runner import docker_ready

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("prguard")

app = FastAPI(title="PRGuard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("PRGUARD_CORS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_TOKEN = os.environ.get("PRGUARD_API_TOKEN", "").strip()


def require_token(request: Request) -> None:
    """Operator endpoints need PRGUARD_API_TOKEN when it is set (always set it outside localhost)."""
    if not API_TOKEN:
        return
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not hmac.compare_digest(supplied, API_TOKEN):
        raise HTTPException(401, "missing or wrong PRGuard API token")


SERVER_SANDBOX = os.environ.get("PRGUARD_SANDBOX", "1").lower() not in ("0", "false", "no")
MAX_JOBS = 200
contexts: dict[str, TaskContext] = {}
results: "OrderedDict[str, CommandResult]" = OrderedDict()
seen_deliveries: "OrderedDict[str, None]" = OrderedDict()
_lock = asyncio.Lock()  # one job at a time: the GPU is the bottleneck anyway


async def _run_job(ctx: TaskContext, command_list: list[str], cfg: RepoConfig | None, question: str = "",
                   publish: bool = True) -> None:
    async with _lock:
        for command in command_list:
            job_ctx = ctx.model_copy(update={"review_id": triggers.new_review_id(), "command": command})
            contexts[job_ctx.review_id] = job_ctx
            try:
                result = await commands.run(job_ctx, command, Options(publish=publish, sandbox=SERVER_SANDBOX, question=question), cfg)
            except Exception as exc:
                log.exception("job %s failed", job_ctx.review_id)
                bus.emit(Event(type="error", review_id=job_ctx.review_id, source="pr", reason=f"{command} failed: {exc}"))
                continue
            results[job_ctx.review_id] = result
            while len(results) > MAX_JOBS:
                old, _ = results.popitem(last=False)
                contexts.pop(old, None)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state", dependencies=[Depends(require_token)])
async def state() -> dict[str, Any]:
    prs = [{"number": n, "title": m["title"]} for n, m in sorted(mock_upstream().prs.items())] if settings.upstream == "mock" else []
    return {
        "enforcer": "POLICY",
        "upstream": settings.upstream,
        "llm": "MOCK LLM" if settings.llm_is_mock else settings.llm_model,
        "llm_url": settings.llm_base_url,
        "checker": settings.checker[2] if settings.checker else "not configured",
        "target_repo": settings.target_repo if settings.upstream == "mock" else "",
        "allowed_repos": settings.allowed_repos,
        "prs": prs,
        "sandbox": (await docker_ready() or "ready") if SERVER_SANDBOX else "disabled",
        "commands": list(COMMANDS),
    }


@app.get("/jobs", dependencies=[Depends(require_token)])
async def jobs() -> list[dict[str, Any]]:
    return [{"review_id": r.review_id, "command": r.command, "repo": r.repo, "pr": r.pr_number, "ok": r.ok,
             "error": r.error, "gate": r.gate, "posted": r.posted_urls, "seconds": r.duration_s,
             "verdict": r.review.verdict if r.review else None} for r in reversed(results.values())]


class RunBody(BaseModel):
    pr_url: str | None = None
    repo: str | None = None
    pr_number: int | None = None
    command: str = "review"
    question: str = ""
    publish: bool = True


@app.post("/run", dependencies=[Depends(require_token)])
async def run(body: RunBody, background: BackgroundTasks) -> dict[str, Any]:
    if body.command not in COMMANDS:
        raise HTTPException(400, f"command must be one of {COMMANDS}")
    if body.pr_url:
        parsed = triggers.parse_pr_url(body.pr_url)
        if not parsed:
            raise HTTPException(400, "pr_url must look like https://github.com/owner/repo/pull/123")
        repo, number = parsed
    elif body.pr_number:
        repo, number = body.repo or settings.target_repo, body.pr_number
    else:
        raise HTTPException(400, "give pr_url, or repo and pr_number")
    if settings.upstream != "mock" and not settings.repo_allowed(repo):
        raise HTTPException(403, f"{repo} is not in PRGUARD_ALLOWED_REPOS")
    try:
        ctx = await triggers.context_for_pr(gateway(), repo, number, sender="operator")
    except UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc
    background.add_task(_run_job, ctx, [body.command], None, body.question, body.publish)
    return {"queued": body.command, "repo": repo, "pr": number, "head_sha": ctx.head_sha}


# Kept for the dashboard's "Run review" button.
class ReviewBody(BaseModel):
    pr_url: str | None = None
    pr_number: int | None = None


@app.post("/review", dependencies=[Depends(require_token)])
async def review(body: ReviewBody, background: BackgroundTasks) -> dict[str, Any]:
    return await run(RunBody(pr_url=body.pr_url, pr_number=body.pr_number), background)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    body = await request.body()
    if not triggers.verify_signature(settings.github_webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if delivery in seen_deliveries:
        return {"status": "duplicate"}
    seen_deliveries[delivery] = None
    while len(seen_deliveries) > 5000:
        seen_deliveries.popitem(last=False)
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    payload = json.loads(body)
    repo = (payload.get("repository") or {}).get("full_name", "")
    if not settings.repo_allowed(repo):
        return {"status": "ignored", "reason": f"{repo} is not in PRGUARD_ALLOWED_REPOS"}
    try:
        job, reason = await triggers.job_from_event(event, payload, gateway())
    except UpstreamError as exc:
        return {"status": "error", "reason": str(exc)}
    if job is None:
        return {"status": "ignored", "reason": reason}
    background.add_task(_run_job, job.ctx, job.commands, job.cfg, job.question)
    return {"status": "queued", "commands": job.commands, "trigger": job.trigger}


@app.post("/approve/{review_id}", dependencies=[Depends(require_token)])
async def approve(review_id: str) -> dict[str, Any]:
    ctx, result = contexts.get(review_id), results.get(review_id)
    if ctx is None or result is None or result.review is None:
        raise HTTPException(404, "unknown or unfinished review")
    if result.review.verdict != "NEEDS_HUMAN":
        raise HTTPException(409, "this review is not waiting for a human")
    # Runtime permission change, scoped to this one task.
    ctx.human_approved = True
    bus.emit(Event(type="hitl.approved", review_id=review_id, source="operator",
                   reason=f"human approved; APPROVE unlocked for {ctx.repo_full_name}#{ctx.pr_number} only"))
    args = {"repo": ctx.repo_full_name, "pull_number": ctx.pr_number, "event": "APPROVE",
            "body": "Approved by a human reviewer via PRGuard."}
    try:
        res = await gateway().call(aggregator.AGENT_ID, ctx, "create_pull_request_review", args)
    except UpstreamError as exc:
        return {"approved": True, "posted": False, "reason": f"allowed by policy, refused by GitHub: {exc}"}
    if isinstance(res, Denial):
        return {"approved": True, "posted": False, "reason": res.reason}
    url = res.data.get("html_url", "") if isinstance(res.data, dict) else ""
    bus.emit(Event(type="review.posted", review_id=review_id, source=aggregator.AGENT_ID, payload={"url": url, "event": "APPROVE"}))
    return {"approved": True, "posted": True, "url": url}


@app.post("/reset", dependencies=[Depends(require_token)])
async def reset() -> dict[str, Any]:
    """Clear the dashboard feed (and recorded writes when using the mock GitHub)."""
    bus.clear()
    if settings.upstream == "mock":
        mock_upstream().reset()
    return {"ok": True}


@app.get("/config/{owner}/{name}/{pr}", dependencies=[Depends(require_token)])
async def show_config(owner: str, name: str, pr: int) -> dict[str, Any]:
    """The effective .prguard.toml for a PR (read from its base branch)."""
    repo = f"{owner}/{name}"
    if settings.upstream != "mock" and not settings.repo_allowed(repo):
        raise HTTPException(403, f"{repo} is not in PRGUARD_ALLOWED_REPOS")
    try:
        ctx = await triggers.context_for_pr(gateway(), repo, pr)
    except UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc
    cfg = await load_config(ctx, gateway())
    from dataclasses import asdict

    return asdict(cfg)


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    if API_TOKEN and not hmac.compare_digest(socket.query_params.get("token", ""), API_TOKEN):
        await socket.close(code=4401)
        return
    await socket.accept()
    queue = bus.subscribe()
    try:
        for event in list(bus.history):
            await socket.send_text(event.model_dump_json())
        while True:
            event = await queue.get()
            await socket.send_text(event.model_dump_json())
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        bus.unsubscribe(queue)


# The built dashboard (frontend/dist), served at / when present.
if (settings.ui_dist_dir / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=settings.ui_dist_dir / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(settings.ui_dist_dir / "index.html")
