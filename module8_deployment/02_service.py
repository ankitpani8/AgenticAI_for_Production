"""Module 8 — the service shell: the agent, over HTTP, ready for an orchestrator.

Module 5 already put the agent behind FastAPI. This version adds the two things
an orchestrator (Kubernetes, Cloud Run) actually needs to run it safely:

  GET /health   LIVENESS  -- "is the process alive?" Cheap, always answers if
                            the event loop is running. If this fails, the
                            orchestrator RESTARTS the pod.
  GET /ready    READINESS -- "can I serve a real request right now?" For an
                            agent that means: has a model provider been health-
                            checked and BOUND? If this fails, the orchestrator
                            keeps the pod running but routes NO traffic to it
                            until it passes. This maps directly onto the startup
                            health-check protocol in lib/providers.py.

The distinction matters: a pod can be alive (liveness OK) but not yet ready
(no model bound). Wiring both to the same endpoint -- a common mistake -- makes
the orchestrator restart pods that are merely still warming up.

Also here: graceful shutdown (drain in-flight requests on SIGTERM, which is how
every orchestrator asks a container to stop), and structured JSON logs to
stdout (the container platform collects stdout; no log files to manage).

Run:  python 02_service.py         then  curl localhost:8080/health
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# Import the sibling agent. The filename starts with a digit, so a plain
# `import 01_agent` is a syntax error -- load it by string. (In the real repo
# you'd rename to a valid identifier or use a package; kept faithful to the
# repo's numbered-file convention here.)
agent = importlib.import_module("01_agent")


# ---------------------------------------------------------------------------
# Structured logging to stdout as JSON lines.
# ---------------------------------------------------------------------------

def log_json(level: str, msg: str, **fields) -> None:
    print(json.dumps({"log": "service", "level": level, "msg": msg, **fields}), flush=True)


# ---------------------------------------------------------------------------
# Lifespan: startup binds a model (best-effort); shutdown drains gracefully.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Config comes from the environment (12-factor). Never hardcode.
    port = os.environ.get("PORT", "8080")
    log_json("info", "starting up", port=port)

    # Best-effort model bind at startup. On a long-running pod this pays the
    # health-check cost once. It is NOT fatal if it fails here -- /ready will
    # report not-ready and the orchestrator will hold traffic until it passes.
    bound = agent.ensure_model()
    log_json("info", "startup model bind", bound=bound, provider=agent._MODEL_LABEL)

    log_json("info", "ready to accept traffic")
    yield
    # SIGTERM lands here. FastAPI/uvicorn stops accepting new requests and lets
    # in-flight ones finish before this block completes. Close DB pools etc here.
    log_json("info", "shutting down (draining in-flight requests)")


app = FastAPI(title="Agentic Deployment Service", version="0.8.0", lifespan=lifespan)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class QueryResponse(BaseModel):
    answer: str
    provider: str | None = None


# ---------------------------------------------------------------------------
# Liveness vs readiness.
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """LIVENESS. Cheap. If the process can answer this, it should not be killed."""
    return {"status": "alive"}


@app.get("/ready")
async def ready():
    """READINESS. Reports whether a model is bound. 503 => hold traffic.

    We attempt a bind on each check so a pod that started before its provider
    was reachable can become ready later without a restart."""
    ok = agent.ensure_model()
    body = {
        "ready": ok,
        "provider": agent._MODEL_LABEL,
        "cache": agent.cache_stats(),
    }
    if not ok:
        # 503 is the signal an orchestrator's readiness probe expects for
        # "not ready yet". The pod stays up; it just receives no traffic.
        return JSONResponse(status_code=503, content=body)
    return body


# ---------------------------------------------------------------------------
# The actual work.
# ---------------------------------------------------------------------------

@app.post("/invoke", response_model=QueryResponse)
async def invoke(req: QueryRequest):
    """Non-streaming answer."""
    try:
        ans = await agent.answer_async(req.query)
        return QueryResponse(answer=ans, provider=agent._MODEL_LABEL)
    except Exception as e:
        # Honest 503 when it's an availability problem (no provider), 500 otherwise.
        code = 503 if isinstance(e, agent.TransientError) else 500
        raise HTTPException(status_code=code, detail=str(e))


@app.post("/invoke/stream")
async def invoke_stream(req: QueryRequest):
    """Streaming answer via Server-Sent Events."""
    async def gen():
        try:
            async for chunk in agent.stream_answer_async(req.query):
                yield {"event": "token", "data": chunk}
            yield {"event": "done", "data": ""}
        except Exception as e:
            yield {"event": "error", "data": str(e)}
    return EventSourceResponse(gen())


if __name__ == "__main__":
    import uvicorn
    # Bind 0.0.0.0 (not 127.0.0.1): inside a container the process must accept
    # connections from outside the container's network namespace. PORT from env
    # so the platform (Cloud Run sets $PORT) controls it.
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("02_service:app", host="0.0.0.0", port=port, reload=False)
