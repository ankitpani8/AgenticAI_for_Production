"""Module 8 — the agent we are going to ship.

This is a deliberately SELF-CONTAINED version of the Module 5 async RAG agent.
Modules 1-7 built these patterns across many files with cross-module imports
and a real vector store. For a deployment module, we want one file that runs
anywhere with no sibling-module imports, no ChromaDB, and no filesystem state
-- because every one of those is a thing that can break inside a container or
a scale-to-zero cold start. So we INLINE trimmed versions of:

  - telemetry  (Module 5: JSON-lines to stdout -- ideal for container logs)
  - retry      (Module 5: classify transient vs permanent, backoff)
  - cache      (Module 5: keep only the in-memory exact-match layer; the
                semantic/ChromaDB layer needs a volume, so we drop it here)
  - retrieval  (Module 4: replaced by a tiny inline fact set -- zero deps)

The model-selection convention is the REAL one from lib/providers.py:
request a model by ROLE, health-check the preference chain, bind the first
that answers. The important deployment change: binding is LAZY and tolerant.
The process must import and start even when NO provider is reachable (no keys
yet, Ollama not up). Whether a model is bound is exactly what the /ready probe
reports -- see 02_service.py.

Run standalone:  python 01_agent.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import AsyncIterator, Optional

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Inlined telemetry (Module 5 pattern). One JSON line per request to stdout.
# A container platform (Docker, K8s, Cloud Run) collects stdout automatically,
# so "structured logging" in a container is often just: print JSON to stdout.
# ---------------------------------------------------------------------------

@dataclass
class RequestTelemetry:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_query: str = ""
    started_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    cache_hit: Optional[str] = None
    llm_calls: int = 0
    provider: Optional[str] = None
    error: Optional[str] = None

    def finalize(self) -> None:
        self.duration_ms = round((time.time() - self.started_at) * 1000, 2)

    def emit(self) -> None:
        print(json.dumps({"log": "telemetry", **asdict(self)}), flush=True)


class _Telemetry:
    """Context manager so finalize/emit always runs, even on error."""
    def __init__(self, query: str):
        self.t = RequestTelemetry(user_query=query[:200])

    def __enter__(self) -> RequestTelemetry:
        return self.t

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.t.error = f"{exc_type.__name__}: {str(exc)[:200]}"
        self.t.finalize()
        self.t.emit()
        return False  # never swallow the exception


# ---------------------------------------------------------------------------
# Inlined retry (Module 5 pattern). Classify, then retry only transient/rate.
# ---------------------------------------------------------------------------

class TransientError(Exception):
    """Network blip, 5xx, timeout. Safe to retry."""

class RateLimitError(Exception):
    """429 / quota. Retry after backoff."""

class PermanentError(Exception):
    """4xx (except 429), auth, schema. Do NOT retry."""


def classify_exception(exc: Exception) -> type[Exception]:
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "quota" in msg:
        return RateLimitError
    if "401" in msg or "403" in msg or "invalid api key" in msg:
        return PermanentError
    if "400" in msg or "schema" in msg or "validation" in msg:
        return PermanentError
    return TransientError


async def call_with_retry(coro_factory, max_attempts: int = 3):
    """Run a no-arg async callable with classify-based retry + backoff.

    coro_factory is a lambda returning a fresh coroutine each attempt:
        await call_with_retry(lambda: model.ainvoke(msgs))
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await coro_factory()
        except Exception as e:
            kind = classify_exception(e)
            if kind is PermanentError or attempt >= max_attempts:
                raise
            delay = min(2 ** (attempt - 1), 8) + (0.1 * attempt)
            log.warning("retryable %s (attempt %d/%d), backing off %.1fs: %s",
                        kind.__name__, attempt, max_attempts, delay, str(e)[:80])
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Inlined exact cache (Module 5 layer 1 only). In-memory, TTL'd. No vector DB.
# ---------------------------------------------------------------------------

_EXACT_CACHE: dict[str, tuple[str, float]] = {}
_EXACT_TTL = 3600.0
_STATS = {"hits": 0, "misses": 0}


def cache_get(query: str) -> Optional[str]:
    entry = _EXACT_CACHE.get(query)
    if entry:
        resp, ts = entry
        if time.time() - ts < _EXACT_TTL:
            _STATS["hits"] += 1
            return resp
        del _EXACT_CACHE[query]
    _STATS["misses"] += 1
    return None


def cache_store(query: str, response: str) -> None:
    _EXACT_CACHE[query] = (response, time.time())


def cache_stats() -> dict:
    total = _STATS["hits"] + _STATS["misses"]
    return {"size": len(_EXACT_CACHE), **_STATS,
            "hit_rate": round(_STATS["hits"] / total, 3) if total else 0.0}


def cache_clear() -> None:
    _EXACT_CACHE.clear()


# ---------------------------------------------------------------------------
# Inline "knowledge base" (replaces Module 4's RAG). A handful of facts so the
# agent can answer grounded questions with ZERO external dependencies. In a
# real deployment this is your vector store -- a networked dependency the
# readiness probe should also check.
# ---------------------------------------------------------------------------

_KNOWLEDGE = {
    "deployment": "Deployment is the process of taking a working agent and "
                  "making it reachable and reliable for others: a network "
                  "boundary, a container, an orchestrator, and a cloud target.",
    "readiness": "A readiness probe reports whether a service can serve traffic "
                 "right now. For an agent, 'ready' means a model provider has "
                 "been health-checked and bound.",
    "scale to zero": "Scale-to-zero means an idle service runs no instances and "
                     "costs nothing until a request arrives, at the cost of a "
                     "cold-start latency on the first request.",
}


def retrieve(query: str, k: int = 2) -> list[str]:
    """Trivial keyword retrieval over the inline facts. Deterministic, no deps."""
    q = query.lower()
    hits = [text for key, text in _KNOWLEDGE.items() if key in q]
    return hits[:k]


# ---------------------------------------------------------------------------
# Lazy, tolerant model binding using the REAL role convention.
# ---------------------------------------------------------------------------

_MODEL = None          # bound BaseChatModel, or None if nothing reachable
_MODEL_LABEL = None    # e.g. "gemini:gemini-2.5-flash"
_BIND_ATTEMPTED = False


def _try_bind_model():
    """Attempt to bind a 'heavy'-role model via lib/providers.py conventions.

    Tolerant by design: if lib.providers isn't importable, or no provider is
    reachable, we return (None, None) instead of raising. The service can still
    start and serve /health; /ready will report not-ready until a model binds.
    """
    global _BIND_ATTEMPTED
    _BIND_ATTEMPTED = True
    try:
        # In the real repo this is `from lib.providers import select_all_models`.
        # Kept as a guarded import so this file runs even outside the repo.
        from lib.providers import select_all_models  # type: ignore
    except Exception as e:
        log.warning("provider policy unavailable (%s); running unbound", type(e).__name__)
        return None, None
    try:
        selections = select_all_models(roles=["heavy"])
        sel = selections["heavy"]
        model = sel.to_langchain(temperature=0.3)
        return model, f"{sel.provider}:{sel.name}"
    except Exception as e:
        log.warning("no provider bound (%s: %s)", type(e).__name__, str(e)[:80])
        return None, None


def ensure_model() -> bool:
    """Bind a model if not already bound. Returns True if a model is available.
    Called by the readiness probe and before answering."""
    global _MODEL, _MODEL_LABEL
    if _MODEL is not None:
        return True
    _MODEL, _MODEL_LABEL = _try_bind_model()
    return _MODEL is not None


def is_ready() -> bool:
    """Readiness = a model has been bound. Cheap, no network call after bind."""
    return _MODEL is not None


SYSTEM_PROMPT = (
    "You are a concise assistant. Answer using the provided context. "
    "If the context does not contain the answer, say so plainly -- do not invent facts."
)


def _build_messages(query: str):
    from langchain_core.messages import SystemMessage, HumanMessage
    ctx = retrieve(query)
    sys = SYSTEM_PROMPT
    if ctx:
        sys += "\n\n## Context\n" + "\n\n".join(ctx)
    else:
        sys += "\n\nNo context retrieved. If the query needs facts, say you don't know."
    return [SystemMessage(content=sys), HumanMessage(content=query)]


async def answer_async(query: str) -> str:
    """Non-streaming answer. Cache -> retrieve -> LLM (with retry)."""
    with _Telemetry(query) as t:
        if (cached := cache_get(query)) is not None:
            t.cache_hit = "exact"
            return cached

        if not ensure_model():
            # No provider reachable. Honest failure, not a fabricated answer.
            raise TransientError("no model provider available (check keys / Ollama)")

        t.provider = _MODEL_LABEL
        messages = _build_messages(query)
        response = await call_with_retry(lambda: _MODEL.ainvoke(messages))
        t.llm_calls = 1
        content = getattr(response, "content", str(response))
        cache_store(query, content)
        return content


async def stream_answer_async(query: str) -> AsyncIterator[str]:
    """Streaming answer via astream. No retry mid-stream (would re-emit)."""
    with _Telemetry(query) as t:
        if (cached := cache_get(query)) is not None:
            t.cache_hit = "exact"
            for i in range(0, len(cached), 50):
                yield cached[i:i + 50]
            return

        if not ensure_model():
            raise TransientError("no model provider available (check keys / Ollama)")

        t.provider = _MODEL_LABEL
        messages = _build_messages(query)
        t.llm_calls = 1
        full = ""
        async for chunk in _MODEL.astream(messages):
            tok = getattr(chunk, "content", "")
            if tok:
                full += tok
                yield tok
        cache_store(query, full)


# ---------------------------------------------------------------------------
# Standalone self-test / demo.
# ---------------------------------------------------------------------------

async def _demo() -> None:
    print("PASS import: agent module loaded")

    # 1) Retrieval is deterministic and dependency-free.
    ctx = retrieve("tell me about deployment and readiness")
    assert len(ctx) == 2, f"expected 2 context hits, got {len(ctx)}"
    print("PASS retrieve: 2 grounded facts returned")

    # 2) Cache round-trips.
    cache_store("q", "cached-answer")
    assert cache_get("q") == "cached-answer"
    print("PASS cache: exact-match store/get works")

    # 3) Retry classifies permanent errors and does NOT retry them.
    calls = {"n": 0}
    async def perm():
        calls["n"] += 1
        raise Exception("401 invalid api key")
    try:
        await call_with_retry(perm, max_attempts=3)
    except Exception:
        pass
    assert calls["n"] == 1, f"permanent error should not retry, got {calls['n']} calls"
    print("PASS retry: permanent (401) error fails fast, no retry")

    # 4) Retry retries transient errors up to max_attempts.
    calls2 = {"n": 0}
    async def transient():
        calls2["n"] += 1
        raise Exception("503 service unavailable")
    try:
        await call_with_retry(transient, max_attempts=3)
    except Exception:
        pass
    assert calls2["n"] == 3, f"transient should retry to max, got {calls2['n']}"
    print("PASS retry: transient (503) error retried to max attempts")

    # 5) Readiness is False before any model is bound (no keys in sandbox).
    #    ensure_model() is tolerant: returns False rather than raising.
    bound = ensure_model()
    print(f"PASS readiness: model bound = {bound} "
          f"({'a provider was reachable' if bound else 'no provider in this env, as expected'})")

    # 6) answer_async fails honestly (not fabricated) when unbound.
    if not bound:
        try:
            await answer_async("what is scale to zero?")
            raise AssertionError("should have raised without a provider")
        except TransientError:
            print("PASS answer: honest failure when no provider (no fabrication)")

    print("PASS telemetry: JSON lines emitted above (see 'telemetry' entries)")
    print("\nSELF-TEST COMPLETE")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    asyncio.run(_demo())
