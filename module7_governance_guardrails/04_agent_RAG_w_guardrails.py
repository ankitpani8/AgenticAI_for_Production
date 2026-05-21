"""Module 5 service with all guardrails wired in.

Pipeline per request:
  1. Input guardrails (PII redaction, injection detection, length check)
  2. Agent (RAG + answer)
  3. Output guardrails (PII leak detection, topic-scope check)
  4. Tool calls (when made) gated by allowlist + approval
  5. Audit log throughout
"""
import importlib
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent / "module5_production"))

_agent = importlib.import_module("04_agent_RAG")
_input_gr = importlib.import_module("01_input_guardrails")
_output_gr = importlib.import_module("02_output_guardrails")
_tool_gr = importlib.import_module("03_tool_guardrails")


app = FastAPI(title="RAG Agentic Service with Guardrails", version="0.7.0")


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)


class QueryResponse(BaseModel):
    answer: str
    guardrail_events: list[str] = []  # what fired


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ask", response_model=QueryResponse)
async def ask(req: QueryRequest):
    events = []

    # Input guardrails
    input_check = _input_gr.validate_input(req.query)
    if not input_check.valid:
        _tool_gr.audit({"event": "input_blocked",
                        "query": req.query[:200],
                        "reason": input_check.reason})
        events.append(f"input_blocked:{input_check.reason}")
        raise HTTPException(status_code=400,
                            detail={"error": "input rejected",
                                    "reason": input_check.reason,
                                    "events": events})

    if input_check.detected_pii:
        events.append(f"pii_redacted:{input_check.detected_pii}")

    # Use the sanitized input for the agent (PII already redacted)
    raw_answer = await _agent.answer_async(input_check.sanitized_input)

    # Output guardrails
    output_check = _output_gr.validate_output(raw_answer)
    if not output_check.valid:
        _tool_gr.audit({"event": "output_blocked",
                        "query": req.query[:200],
                        "reason": output_check.reason})
        events.append(f"output_blocked:{output_check.reason}")
        # We sanitize rather than reject -- depends on threat model
        return QueryResponse(
            answer="(response withheld by output guardrail)",
            guardrail_events=events,
        )

    if output_check.detected_pii:
        events.append(f"output_pii_redacted:{output_check.detected_pii}")
        raw_answer = output_check.sanitized_output

    _tool_gr.audit({"event": "request_completed",
                    "query": req.query[:200], "events": events})
    return QueryResponse(answer=raw_answer, guardrail_events=events)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("04_agent_RAG_w_guardrails:app", host="127.0.0.1", port=8001, reload=False)