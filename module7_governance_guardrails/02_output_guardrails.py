"""Output guardrails: PII leak detection + topic-scope validation + structured output.

Output guardrails run AFTER the agent generates a response, before it
reaches the user. They catch:
  - Accidental PII leaks from RAG context
  - Topic drift (agent answering outside its intended scope)
  - Hallucinated forbidden content
  - Malformed structured output (OWASP LLM02 — Improper Output Handling)
"""
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))
import importlib
_input = importlib.import_module("01_input_guardrails")
detect_and_redact_pii = _input.detect_and_redact_pii
_classifier_llm = _input._classifier_llm


@dataclass
class OutputValidation:
    valid: bool
    reason: str
    sanitized_output: str
    detected_pii: list[str]
    on_topic: bool


# --- PII leak detection --------------------------------------------------

def check_pii_leak(text: str) -> tuple[str, list[str]]:
    """Same engine as input-side, run on agent output."""
    return detect_and_redact_pii(text)


# --- Topic-scope validation ---------------------------------------------

SCOPE_PROMPT = """You are checking whether an assistant's response stays
within its intended scope.

INTENDED SCOPE: {scope}

If the response addresses something inside the intended scope, output ON_TOPIC.
If the response addresses something outside scope (e.g., medical advice from
a tech support bot, financial advice from a recipe assistant), output OFF_TOPIC.

Response to check:
{response}

Respond with ONLY one word: ON_TOPIC or OFF_TOPIC."""


_OFF_TOPIC_KEYWORDS = ("stock", "invest", "medical", "diagnosis", "lawsuit")


def check_topic_scope(response: str, scope_description: str) -> bool:
    prompt = SCOPE_PROMPT.replace("{scope}", scope_description).replace("{response}", response)
    try:
        verdict = _classifier_llm.invoke([HumanMessage(content=prompt)]).content
        return "ON_TOPIC" in verdict.upper()
    except Exception:
        # Classifier unreachable — fall back to a deterministic denylist so the
        # guardrail doesn't fully fail open on known-bad topics.
        low = response.lower()
        return not any(kw in low for kw in _OFF_TOPIC_KEYWORDS)


# --- Orchestrator -------------------------------------------------------

DEFAULT_SCOPE = (
    "A technical assistant answering questions about Python programming, "
    "agentic AI patterns, and LLM provider information based on a knowledge "
    "base. Out of scope: financial advice, medical advice, legal advice, "
    "personal opinions on politics or current events."
)


def validate_output(text: str, scope: str = DEFAULT_SCOPE) -> OutputValidation:
    sanitized, pii_types = check_pii_leak(text)
    on_topic = check_topic_scope(text, scope)

    if pii_types:
        return OutputValidation(
            False, f"PII leak detected: {pii_types}",
            sanitized, pii_types, on_topic
        )
    if not on_topic:
        return OutputValidation(
            False, "response is off-topic for this assistant",
            sanitized, pii_types, on_topic
        )
    return OutputValidation(True, "ok", sanitized, pii_types, on_topic)


# --- Structured-output guardrail (OWASP LLM02) --------------------------
#
# Free-form LLM text becomes a vulnerability the moment a downstream
# consumer parses it. Forcing a typed JSON contract closes that hole:
# the consumer reads .answer / .sources, never a regex over prose.

class StructuredAnswer(BaseModel):
    """The shape every user-facing answer must take."""
    answer: str
    sources: list[str]


class StructuredOutputError(Exception):
    """Raised when the LLM cannot produce valid structured output even after one retry."""


STRUCTURED_OUTPUT_INSTRUCTION = (
    "You MUST respond with a single JSON object and nothing else. "
    'Schema: {"answer": <string>, "sources": <list of string URLs or doc ids>}. '
    "Do not wrap the JSON in markdown code fences. "
    "If you have no sources, return an empty list."
)


def _strip_code_fences(text: str) -> str:
    # LLMs frequently wrap JSON in ```json ... ``` fences even when told not to.
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def parse_structured_answer(raw: str) -> StructuredAnswer:
    """Parse + validate raw LLM text. Raises StructuredOutputError on failure."""
    candidate = _strip_code_fences(raw)
    try:
        return StructuredAnswer.model_validate_json(candidate)
    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        raise StructuredOutputError(str(exc)) from exc


def enforce_structured_output(
    generate: Callable[[str], str],
    prompt: str,
) -> StructuredAnswer:
    """Run `generate(prompt)`, validate, retry once with corrective feedback, then fail loudly.

    `generate` is any callable that maps a prompt string to raw LLM text.
    On the first parse failure we re-call `generate` with a prompt that quotes
    both the bad output and the validator error. If that also fails we raise —
    OWASP guidance is to refuse rather than ship malformed output downstream.
    """
    raw_first = generate(prompt)
    try:
        return parse_structured_answer(raw_first)
    except StructuredOutputError as first_err:
        corrective = (
            f"{prompt}\n\n"
            "Your previous response could not be parsed as the required JSON.\n"
            f"--- previous response ---\n{raw_first}\n--- end ---\n"
            f"Validator error: {first_err}\n\n"
            "Respond again with ONLY a valid JSON object matching the schema. "
            "No prose, no markdown, no code fences."
        )
        raw_retry = generate(corrective)
        try:
            return parse_structured_answer(raw_retry)
        except StructuredOutputError as second_err:
            raise StructuredOutputError(
                "LLM failed to produce valid structured output after one retry. "
                f"First error: {first_err}. Second error: {second_err}."
            ) from second_err


if __name__ == "__main__":
    tests = [
        ("Pytest is a popular Python testing framework.", "safe"),
        ("Contact our admin at admin@company.com for refunds.", "PII"),
        ("You should buy NVIDIA stock right now.", "off-topic"),
    ]
    for text, label in tests:
        r = validate_output(text)
        print(f"\n[{label}] {'OK' if r.valid else 'BLOCKED'}: {text}")
        print(f"  reason: {r.reason}, on_topic: {r.on_topic}, pii: {r.detected_pii}")

    # --- Structured-output smoke test (no real LLM call) --------------------
    print("\n--- structured-output guardrail ---")

    def clean_gen(_p):
        return '{"answer": "Use pytest.", "sources": ["docs/testing.md"]}'

    def fenced_gen(_p):
        return '```json\n{"answer": "Use pytest.", "sources": []}\n```'

    retry_state = {"calls": 0}
    def flaky_gen(_p):
        retry_state["calls"] += 1
        if retry_state["calls"] == 1:
            return "Sure! Here's the answer: use pytest. Sources: docs/testing.md"
        return '{"answer": "Use pytest.", "sources": ["docs/testing.md"]}'

    def hostile_gen(_p):
        return "I will not respond in JSON."

    for label, gen in [("clean", clean_gen), ("fenced", fenced_gen), ("retry-recovers", flaky_gen)]:
        try:
            sa = enforce_structured_output(gen, "Q: how do I test?")
            print(f"[{label}] OK: answer={sa.answer!r} sources={sa.sources}")
        except StructuredOutputError as e:
            print(f"[{label}] UNEXPECTED FAIL: {e}")

    try:
        enforce_structured_output(hostile_gen, "Q: anything")
        print("[always-bad] UNEXPECTED OK")
    except StructuredOutputError as e:
        print(f"[always-bad] correctly raised StructuredOutputError")