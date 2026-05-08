"""
runtime/monitor/live.py
Agent Governance v2 — Triggered Health Reader

NOT a continuously running process.
Runs ONLY when triggered by an ESCALATE signal from the interceptor.

Reads the last N entries from the execution_log and heartbeat_log
for a session and gives the Orchestrator a structured health summary
to act on. The Orchestrator uses this to decide:
    - Reassign task to sibling agent
    - Spawn fresh agent for failed task
    - Continue with partial results
    - Escalate further to human

Design: The audit log is a ledger (what happened).
        The live monitor is a snapshot (what is happening right now).
        They serve different purposes and must not be conflated.
"""

from datetime import datetime, timezone
from typing import Optional

from runtime.schema import get_connection
from runtime.monitor.heartbeat import get_progress_summary


def get_session_health(session_id: str, last_n: int = 20) -> dict:
    """
    Read the last N audit entries for a session and return a health summary.

    Called by the Orchestrator when it receives an ESCALATE signal.
    Not called during normal operation — only triggered on ESCALATE.

    Args:
        session_id: The session to inspect.
        last_n:     How many recent execution_log entries to examine.

    Returns:
        {
            "session_id":       str,
            "health":           "OK" | "STUCK" | "LOOPING" | "DEGRADED",
            "recent_blocked":   int,
            "recent_allowed":   int,
            "recent_escalated": int,
            "last_progress":    ISO8601 timestamp or None,
            "stuck_tools":      [ {"tool": str, "action": str}, ... ],
            "budget_remaining": float or None,
            "recommended_action": str,
        }
    """
    conn = get_connection()
    try:
        entries = conn.execute(
            """
            SELECT tool, action, result, reason, timestamp
            FROM execution_log
            WHERE session_id = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (session_id, last_n),
        ).fetchall()

        # Read budget state
        budget_spent = conn.execute(
            """
            SELECT constraint_value FROM constraints
            WHERE session_id = ? AND constraint_key = 'budget_spent'
            ORDER BY priority_level ASC, set_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

        budget_limit = conn.execute(
            """
            SELECT constraint_value FROM constraints
            WHERE session_id = ? AND constraint_key = 'budget_limit'
            ORDER BY priority_level ASC, set_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    # Tally results
    blocked_count   = sum(1 for e in entries if e["result"] == "BLOCKED")
    allowed_count   = sum(1 for e in entries if e["result"] == "ALLOWED")
    escalate_count  = sum(1 for e in entries if e["result"] == "ESCALATE")
    last_allowed    = next((e for e in entries if e["result"] == "ALLOWED"), None)

    # Compute budget remaining
    budget_remaining = None
    if budget_spent and budget_limit:
        import json
        try:
            spent = json.loads(budget_spent["constraint_value"])
            limit = json.loads(budget_limit["constraint_value"])
            budget_remaining = limit - spent
        except Exception:
            pass

    # Get heartbeat progress summary
    progress = get_progress_summary(session_id)
    stuck_tools = progress.get("stuck_tools", [])

    # Determine health status
    if escalate_count > 0 and stuck_tools:
        health = "LOOPING"
    elif escalate_count > 0:
        health = "STUCK"
    elif blocked_count > allowed_count * 2:
        health = "DEGRADED"
    else:
        health = "OK"

    # Recommended action for Orchestrator
    if health == "LOOPING":
        recommended_action = "reassign_to_sibling"
    elif health == "STUCK":
        recommended_action = "retry_with_fresh_agent"
    elif health == "DEGRADED":
        recommended_action = "check_tool_availability"
    else:
        recommended_action = "continue"

    return {
        "session_id":         session_id,
        "health":             health,
        "recent_blocked":     blocked_count,
        "recent_allowed":     allowed_count,
        "recent_escalated":   escalate_count,
        "last_progress":      last_allowed["timestamp"] if last_allowed else None,
        "stuck_tools":        stuck_tools,
        "budget_remaining":   budget_remaining,
        "recommended_action": recommended_action,
    }


def should_notify_human(session_id: str) -> tuple[bool, Optional[str]]:
    """
    Determine if the Orchestrator should escalate further to a human.

    Returns (True, reason) if human notification is warranted.
    Returns (False, None) if Orchestrator can handle it autonomously.

    Human notification is warranted when:
    1. Budget is nearly exhausted (< 10% remaining)
    2. Health is LOOPING and Orchestrator has already tried reassignment
    3. No allowed actions in the last 20 entries (complete stall)
    """
    health = get_session_health(session_id)

    # Budget critical
    if health["budget_remaining"] is not None:
        if health["budget_remaining"] < 0:
            return True, "budget_exhausted: cannot continue without reallocation"

    # Complete stall — no progress at all
    if health["recent_allowed"] == 0 and health["recent_blocked"] > 0:
        return True, (
            f"complete_stall: {health['recent_blocked']} blocks, "
            f"0 allowed actions in last {health['recent_blocked'] + health['recent_allowed']} entries"
        )

    return False, None