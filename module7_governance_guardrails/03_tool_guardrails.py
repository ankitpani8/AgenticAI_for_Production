"""Tool guardrails: allowlist per role, approval flow for high-stakes
actions, audit log for everything.

This is the OWASP "Excessive Agency" defense made concrete. Even if an
attacker bypasses input filters, they can't make the agent call tools it
isn't allowed to call.
"""
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

AUDIT_LOG_PATH = Path(__file__).parent / "audit.log"


@dataclass
class ToolCallAttempt:
    role: str
    tool_name: str
    args: dict
    user_query: str
    timestamp: float = field(default_factory=time.time)
    approved: bool = False
    reason: str = ""
    result: str | None = None


# --- Allowlist: role -> set of tools it may call ------------------------

ROLE_TOOL_ALLOWLIST = {
    "billing":      {"lookup_invoice", "issue_refund"},
    "engineering":  {"create_ticket"},
    "docs":         {"search_docs"},
    "fallback":     set(),     # fallback handler uses no tools
    "router":       set(),     # router classifies, doesn't act
}


# --- Tools requiring human-in-the-loop approval -------------------------

HIGH_STAKES_TOOLS = {"issue_refund"}  # add: send_email, delete_user, payment, etc.


def requires_approval(tool_name: str) -> bool:
    return tool_name in HIGH_STAKES_TOOLS


# --- The gate --------------------------------------------------------

def can_call(role: str, tool_name: str) -> tuple[bool, str]:
    allowed = ROLE_TOOL_ALLOWLIST.get(role, set())
    if tool_name not in allowed:
        return False, f"role '{role}' is not authorized to call '{tool_name}'"
    return True, "ok"


# --- Audit logging --------------------------------------------------

def audit(event: dict) -> None:
    """Append-only JSON log. In production this goes to a tamper-evident
    store (CloudWatch, Datadog, or a dedicated audit sink)."""
    event["timestamp"] = time.time()
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


# --- Approval workflow (simulated) ----------------------------------

def request_approval(attempt: ToolCallAttempt) -> bool:
    """Synchronous approval prompt. Production version is async with
    Slack/email/UI notification and timeout."""
    print(f"\n[approval-required] {attempt.tool_name}({attempt.args})")
    print(f"  user query: {attempt.user_query[:100]}")
    response = input("  approve? (y/n): ").strip().lower()
    return response in ("y", "yes")


# --- Orchestrator -------------------------------------------------

def gated_tool_call(
    role: str,
    tool_name: str,
    args: dict,
    user_query: str,
    tool_fn: Callable,
    interactive_approval: bool = True,
) -> tuple[bool, str]:
    """Run all tool guardrails, then invoke the tool if allowed."""
    attempt = ToolCallAttempt(role=role, tool_name=tool_name, args=args,
                              user_query=user_query)

    # Layer 1: allowlist
    allowed, reason = can_call(role, tool_name)
    if not allowed:
        attempt.reason = reason
        audit({"event": "tool_blocked", **attempt.__dict__})
        return False, reason

    # Layer 2: approval for high-stakes
    if requires_approval(tool_name):
        if interactive_approval and not request_approval(attempt):
            attempt.reason = "approval denied"
            audit({"event": "approval_denied", **attempt.__dict__})
            return False, "approval denied by reviewer"

    # Execute
    try:
        result = tool_fn(**args)
        attempt.approved = True
        attempt.result = str(result)[:500]
        audit({"event": "tool_executed", **attempt.__dict__})
        return True, str(result)
    except Exception as e:
        attempt.reason = f"execution error: {e}"
        audit({"event": "tool_error", **attempt.__dict__})
        return False, f"tool error: {e}"


if __name__ == "__main__":
    # Demo: docs specialist trying to issue refunds (blocked by allowlist)
    def fake_refund(invoice_id, amount):
        return f"refunded ${amount} on {invoice_id}"

    ok, msg = gated_tool_call(
        role="docs",
        tool_name="issue_refund",
        args={"invoice_id": "INV-1", "amount": 50},
        user_query="please refund me",
        tool_fn=fake_refund,
        interactive_approval=False,
    )
    print(f"Result: ok={ok}, msg={msg}")
    # Check the audit log
    print(f"\nAudit log entries:")
    for line in AUDIT_LOG_PATH.read_text().splitlines()[-3:]:
        print(f"  {line}")