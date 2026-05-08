"""
runtime/monitor/heartbeat.py
Agent Governance v2 — Progress Tracking

Records a progress marker after every ALLOWED tool call.
result_hash = SHA256 of what the tool returned.

If the last N result_hashes for (session_id, tool, action)
are all identical → the agent is stuck. No forward progress.

This solves the demand vs loop problem without caring about intent:
    Legitimate demand:  different results each call → hashes differ → not stuck
    Broken retry loop:  same 503 every call → hashes identical → stuck

Called by AgentBase.call_tool() after every successful tool execution.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from runtime.schema import get_connection

# Number of identical consecutive results required to declare stuck
STUCK_WINDOW = 3


def record_heartbeat(
    session_id: str,
    tool: str,
    action: str,
    tool_result: Any,
) -> None:
    """
    Record a progress marker for this tool call.

    Args:
        session_id:  Session that made the call.
        tool:        Tool name (e.g. 'database').
        action:      Action name (e.g. 'query_records').
        tool_result: Raw result returned by the tool. Hashed for comparison.
                     The actual result is never stored — only its hash.
    """
    result_str  = json.dumps(tool_result, sort_keys=True, default=str)
    result_hash = hashlib.sha256(result_str.encode("utf-8")).hexdigest()
    timestamp   = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO heartbeat_log
                    (session_id, tool, action, result_hash, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, tool, action, result_hash, timestamp),
            )
    except Exception:
        pass  # Heartbeat failure must never affect tool execution
    finally:
        conn.close()


def is_stuck(
    session_id: str,
    tool: str,
    action: str,
    window: int = STUCK_WINDOW,
) -> bool:
    """
    Return True if the agent is making no progress on this tool+action.

    Reads the last `window` heartbeat entries for (session_id, tool, action).
    If all result_hashes are identical → stuck.
    If fewer than `window` entries exist → not enough data → not stuck.

    Args:
        session_id: Session to check.
        tool:       Tool name.
        action:     Action name.
        window:     How many consecutive identical results = stuck.

    Returns:
        True  — agent is stuck (same result repeated `window` times).
        False — agent is making progress or not enough data yet.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT result_hash FROM heartbeat_log
            WHERE session_id = ? AND tool = ? AND action = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (session_id, tool, action, window),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < window:
        return False  # Not enough data to declare stuck

    hashes = [r["result_hash"] for r in rows]
    return len(set(hashes)) == 1  # All identical = stuck


def get_progress_summary(session_id: str) -> dict:
    """
    Return a summary of recent tool activity for this session.

    Used by live.py to give the Orchestrator a picture of what
    the agent has been doing and whether any tools are stuck.

    Returns:
        {
            "total_heartbeats": int,
            "stuck_tools": [ {"tool": str, "action": str}, ... ],
            "last_activity": ISO8601 timestamp or None
        }
    """
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM heartbeat_log WHERE session_id = ?",
            (session_id,),
        ).fetchone()["cnt"]

        last_row = conn.execute(
            """
            SELECT timestamp FROM heartbeat_log
            WHERE session_id = ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

        # Find distinct (tool, action) pairs to check for stuck
        pairs = conn.execute(
            """
            SELECT DISTINCT tool, action FROM heartbeat_log
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    stuck_tools = []
    for pair in pairs:
        if is_stuck(session_id, pair["tool"], pair["action"]):
            stuck_tools.append({"tool": pair["tool"], "action": pair["action"]})

    return {
        "total_heartbeats": total,
        "stuck_tools":      stuck_tools,
        "last_activity":    last_row["timestamp"] if last_row else None,
    }