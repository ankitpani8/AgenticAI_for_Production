# Module 7 — Governance and Guardrails

The defensive side of agent engineering. Previous modules built capability;
this one builds constraint — input/output/tool guardrails layered into the
Module 5 service, plus an adversarial test suite that measures how well the
defenses hold.

## The shift this module makes

Every prior module added something the agent *can* do. This one systematically
adds things it *cannot* do, and ways to detect and refuse attempts to make it.
The mindset shifts from "how do I make it work?" to "how do I make it fail
safely when the input is adversarial?"

## The defense-in-depth principle

The central idea: no single guardrail is sufficient. Real security comes from
layering multiple imperfect defenses so that bypassing all of them is
improbable. Each request passes through:

1. Input guardrails (PII redaction, injection detection, length checks)
2. The agent (with its own refusal behavior from Module 4)
3. Output guardrails (PII leak detection, topic-scope validation)
4. Tool guardrails (allowlist per role, approval for high-stakes actions)
5. Audit logging throughout

When one layer misses, another catches. This module assembles guardrails
that appeared piecemeal across the curriculum (Module 2's validation node,
Module 3's token cap, Module 5's rate limiter) into a coherent posture.

## OWASP LLM Top 10 coverage

This module implements code-level mitigations for:
- **Prompt Injection** — input classifier + structured-content isolation
- **Sensitive Information Disclosure** — PII detection/redaction (Presidio) on
  both input and output
- **Improper Output Handling** — Pydantic schema validation (Challenge 1)
- **Excessive Agency** — tool allowlist per role, enforced architecturally
- **Unbounded Consumption** — length limits, rate limits (from Module 5)

## What's in this module

- **Input guardrails**: PII redaction (Presidio, deterministic), prompt
  injection detection (LLM classifier), length/format checks
- **Output guardrails**: PII leak detection, topic-scope validation with a
  deterministic keyword fallback when the LLM judge is unavailable
- **Tool guardrails**: per-role tool allowlist, human-in-the-loop approval
  for high-stakes actions (refunds), append-only audit log
- **Hardened FastAPI service** wiring all three layers around the Module 5 agent
- **Red-team test suite**: 15 adversarial cases across injection, PII
  extraction, scope violation, excessive agency, and false-positive checks

## Files

```
module7_governance_guardrails/
├── 01_input_guardrails.py     -- PII redaction + injection detection
├── 02_output_guardrails.py    -- PII leak + topic-scope + keyword fallback
├── 03_tool_guardrails.py      -- allowlist + approval flow + audit log
├── 04_hardened_service.py     -- service with all guardrails wired in
├── 05_red_team_dataset.py     -- 15 adversarial test cases
├── 06_red_team_runner.py      -- before/after attack scoring
└── README.md
```

## Findings

### LLM-as-judge guardrails inherit the judge's failure modes
The injection classifier initially flagged a legitimate query ("How should
I manage Python dependencies?") as an attack. The classifier is itself a
small LLM and shares all the pathologies seen earlier in the curriculum:
substitution, over-eagerness, surface-pattern matching. **You cannot defend
an LLM with another LLM without inheriting its weaknesses.** Fixing it required
explicit positive AND negative examples in the classifier prompt — prompt
engineering as security work. This is the strongest argument for combining
deterministic checks (regex, allowlists, length limits) with LLM-based ones.

### Guardrails have an accuracy/safety tradeoff
Tight guardrails block attacks but also block real users; loose guardrails
admit users but miss attacks. There is no perfect setting — you tune against
your threat model and your tolerance for false positives. Overzealous
guardrails that block legitimate requests get disabled by users, after which
real attacks succeed. The false-positive check (3 legitimate queries in the
red-team set) exists precisely to measure this.

### LLM-dependent guardrails inherit the LLM's availability
The topic-scope guardrail makes an LLM call. When that call is unavailable
(rate-limited, timed out), the guardrail's `except` branch decides the
outcome. A fail-open design (return "on-topic" on error) prioritizes
availability but silently disables the guardrail under load. A fail-closed
design prioritizes security but rejects legitimate requests during outages.
The implemented compromise: a deterministic keyword denylist fallback
(`stock`, `invest`, `diagnosis`, etc.) that fires when the LLM judge fails,
so the scope guardrail degrades gracefully rather than disappearing.

### Eval harnesses can under-report defense effectiveness
The red-team runner initially scored a blocked length-abuse attack as
"passed" because it only recognized HTTP 400 (the input-guardrail exception)
and not HTTP 422 (Pydantic schema rejection). The guardrail worked; the
scorer didn't recognize the win. Security test harnesses must account for
every rejection path, or they'll under-count defenses and over-count
vulnerabilities.

### Excessive-agency defense is architectural, not promptable
The tool allowlist enforces that the docs specialist cannot call `issue_refund`
regardless of what the LLM is convinced to want. This is the strongest defense
against prompt injection: even if an attacker fully controls the LLM's intent,
they can't make it call a tool it isn't authorized for. The best defense
against injection is minimizing what a compromised agent can actually do.

### Supply-chain risk is real
The widely-cited `guardrails-ai` library was unavailable on PyPI during this
module's build (quarantined pending review). We used Presidio (stable,
Microsoft-backed) and wrote the rest of the guardrails manually with standard
Python. Fewer dependencies means fewer points of failure — a relevant
trade-off for any production AI system: framework convenience vs supply-chain
control.

### Defense in depth, demonstrated
One out-of-knowledge query was caught not by a guardrail but by the agent's
own factual-refusal behavior (Module 4) before guardrails needed to act. The
innermost layer caught it. This is defense in depth working as designed —
multiple independent layers, any of which can catch a given failure.

## Run it

```bash
cd module7_governance_guardrails

# Smoke-test individual guardrails
python 01_input_guardrails.py
python 02_output_guardrails.py
python 03_tool_guardrails.py

# Start the hardened service (port 8001)
python 04_hardened_service.py

# In another terminal: run the red-team suite
python 06_red_team_runner.py

# Inspect the audit log
Get-Content audit.log -Tail 20
```

Requires Presidio (`presidio-analyzer`, `presidio-anonymizer`, spaCy
`en_core_web_sm`) and the Module 5 agent. The LLM-based guardrails
(injection detection, topic-scope) use the `light` role from
`lib/providers.py`.

## What we deliberately didn't do

- **Differential privacy / training-data-extraction defenses** — model-training
  domain, not agent engineering.
- **Full red-team exercises** — multi-day adversarial engagements with
  creative humans. The automated suite is the engineering version.
- **Regulatory frameworks** (GDPR, HIPAA, EU AI Act) — these dictate which
  guardrails you need; the rules are domain-specific.
- **guardrails-ai / NeMo Guardrails** — referenced as the production
  frameworks that wrap these patterns; we built the primitives by hand.

## What's next

Module 8 — the capstone. Containerize and deploy one end-to-end production
agent that combines everything: the agent logic, the service shell,
observability, evaluation, and guardrails.