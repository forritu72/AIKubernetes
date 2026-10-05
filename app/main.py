"""FastAPI app: static UI, SSE chat, approvals, cluster REST."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import kube, trace
from app.agent import sessions
from app.approvals import registry
from app.config import STATIC_DIR, clusters, settings

trace.configure_logging()
log = logging.getLogger("copilot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.has_llm_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Put a key in .env (get one at "
            "https://platform.openai.com/api-keys), or set OPENAI_BASE_URL to a "
            "local OpenAI-compatible endpoint such as Ollama, which needs no key."
        )
    log.info(
        "model: %s (subagents: %s) via %s",
        settings.agent_model,
        settings.subagent_model_name,
        settings.openai_base_url or "api.openai.com",
    )
    log.info("cluster profile: %s (%s)", clusters.active_name, clusters.active.context)
    log.info(
        "log level %s (set LOG_LEVEL=DEBUG or LOG_TOOL_IO=true in .env for full tool i/o)",
        settings.log_level_name,
    )
    if not settings.has_github:
        log.warning("GITHUB_TOKEN is not set - GitHub tools are disabled this run")
    yield
    await sessions.shutdown()


app = FastAPI(title="Local DevOps Copilot", lifespan=lifespan)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ApprovalRequest(BaseModel):
    request_id: str
    decision: str  # "approve" | "deny"


class SessionRef(BaseModel):
    session_id: str


class ClusterSelect(BaseModel):
    name: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
async def chat(body: ChatRequest) -> StreamingResponse:
    if not body.message.strip():
        raise HTTPException(400, "empty message")
    session = await sessions.get(body.session_id)
    return StreamingResponse(
        session.run_turn(body.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/approve")
async def approve(body: ApprovalRequest) -> dict[str, object]:
    approved = body.decision.lower() in ("approve", "approved", "allow", "yes")
    settled = registry.resolve(body.request_id, approved)
    if not settled:
        # Already timed out, already clicked, or the turn was interrupted.
        raise HTTPException(409, "that approval request is no longer pending")
    return {"ok": True, "approved": approved}


@app.post("/api/interrupt")
async def interrupt(body: SessionRef) -> dict[str, bool]:
    session = sessions.peek(body.session_id)
    if session is None:
        return {"ok": False}
    await session.interrupt()
    return {"ok": True}


@app.post("/api/session/reset")
async def reset_session(body: SessionRef) -> dict[str, bool]:
    session = sessions.peek(body.session_id)
    if session is not None:
        await session.close()
    return {"ok": True}


@app.get("/api/clusters")
async def list_clusters() -> dict[str, object]:
    return {
        "active": clusters.active_name,
        "profiles": [
            {"name": p.name, "label": p.label, "context": p.context}
            for p in clusters.profiles.values()
        ],
    }


@app.post("/api/clusters/select")
async def select_cluster(body: ClusterSelect) -> dict[str, object]:
    try:
        profile = clusters.select(body.name)
    except KeyError:
        raise HTTPException(404, f"no such cluster profile: {body.name}")
    # Sessions carry the old profile's MCP env; drop them so the next turn
    # rebuilds against the new cluster.
    await sessions.reset_all()
    log.info("switched cluster to %s (%s)", profile.name, profile.context)
    return {"ok": True, "active": profile.name, "context": profile.context}


@app.get("/api/cluster/overview")
async def cluster_overview() -> dict[str, object]:
    return await kube.overview(clusters.active)


@app.get("/api/status")
async def status() -> dict[str, object]:
    return {
        "model": settings.agent_model,
        "subagent_model": settings.subagent_model_name,
        "endpoint": settings.openai_base_url or "https://api.openai.com/v1",
        "github": settings.has_github,
        "cluster": clusters.active_name,
        "context": clusters.active.context,
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
