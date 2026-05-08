import json
import re
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Any

from runtime.schema import get_connection

INJECTION_PATTERNS = [
    ("system_prefix", re.compile(r'bssystem s*:', re.IGNORECASE)),
    ("ignore_previous", re.compile(r'ignore s+(previous|prior|all s+previous)', re.IGNORECASE)),
    ("forget_constraints", re.compile(r'forget s+(your s+)?(constraints|instructions|rules)', re.IGNORECASE)),
    ("you_are_now", re.compile(r'you sare s+now s+(a s+different|an? s+)', re.IGNORECASE)),
    ("now_authorized", re.compile(r'(you sare s+)?now s+authorized', re.IGNORECASE)),
    ("disregard", re.compile(r'disregard s+(your s+)?(previous|prior|all)', re.IGNORECASE)),
    ("reauth_inject", re.compile(r'reauth_verified s*[:=] s*(true|1)', re.IGNORECASE)),
    ("budget_limit_inject", re.compile(r'budget_limit s*[:=] s*d', re.IGNORECASE)),
    ("budget_unlimited", re.compile(r'budget s*[:=] s*(unlimited|infinite|none)', re.IGNORECASE)),
    ("pii_clear_inject", re.compile(r'pii_accessed s*[:=] s*(false|0)', re.IGNORECASE)),
    ("shell_exec", re.compile(r'`[^`]{3,}`|\\$\\([^)]+\\)', re.IGNORECASE)),
    ("shell_redirect", re.compile(r'; s*(rm|curl|wget|bash|sh|python|nc) s', re.IGNORECASE)),
    ("prompt_boundary", re.compile(r'< s*/? s*(system|human|assistant|prompt) s*>', re.IGNORECASE)),
    ("llm_instruction", re.compile(r'\\[INST\\]|\\[/INST\\]|<\\|im_start\\|>| <\\|im_end\\|>', re.IGNORECASE)),
]

SANITIZATION_PLACEHOLDER = "[REDACTED_BY_SCANNER]"

def scan_response(session_id: str, tool: str, raw_result: Any) -> tuple[Any, bool]:
    if isinstance(raw_result, str):
        result_str = raw_result
    else:
        try:
            result_str = json.dumps(raw_result, default=str)
        except Exception:
            result_str = str(raw_result)

    matched_patterns = [name for name, pattern in INJECTION_PATTERNS if pattern.search(result_str)]

    if not matched_patterns:
        _write_scan_log(session_id, tool, "CLEAN", None, False)
        return raw_result, False

    pattern_str = ",".join(matched_patterns)
    sanitized = _sanitize(result_str, matched_patterns[0])
    _write_scan_log(session_id, tool, "BLOCKED", pattern_str, True)
    return {
        "_sanitized": True,
        "original_type": type(raw_result).__name__,
        "data": raw_result,
        "message": sanitized,
    }, True

def _sanitize(result_str: str, first_pattern: str) -> str:
    sanitized = result_str
    for _, pattern in INJECTION_PATTERNS:
        sanitized = pattern.sub(SANITIZATION_PLACEHOLDER, sanitized)
    return f"[SCANNER: '{first_pattern}' detected] {sanitized}"

def _write_scan_log(session_id: str, tool: str, scan_result: str, pattern_matched: str | None, sanitized: bool):
    def _append_execution_log(target_session_id: str, action_label: str = "response_scan") -> None:
        sess = conn.execute(
            "SELECT principal_id FROM sessions WHERE session_id = ?",
            (target_session_id,),
        ).fetchone()
        if sess is None:
            return
        prev_row = conn.execute(
            """
            SELECT entry_hash FROM execution_log
            WHERE session_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (target_session_id,),
        ).fetchone()
        prev_hash = prev_row["entry_hash"] if prev_row else "0" * 64
        action_id = str(uuid.uuid4())
        reason = (
            f"response_injection_detected: scanner blocked tool output"
            f"{' [' + pattern_matched + ']' if pattern_matched else ''}"
        )
        chain_input = (
            f"{prev_hash}{action_id}{now_iso}BLOCKED{reason}"
        ).encode("utf-8")
        entry_hash = hashlib.sha256(chain_input).hexdigest()
        conn.execute(
            """
            INSERT INTO execution_log (
                action_id, parent_action_id, session_id, principal_id,
                tool, action, result, reason,
                constraint_version, timestamp, prev_hash, entry_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                None,
                target_session_id,
                sess["principal_id"],
                tool,
                action_label,
                "BLOCKED",
                reason,
                0,
                now_iso,
                prev_hash,
                entry_hash,
            ),
        )

    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """
            INSERT INTO response_scan_log (session_id, tool, scan_result, pattern_matched, sanitized, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, tool, scan_result, pattern_matched, 1 if sanitized else 0, now_iso)
        )

        # Mirror blocked injection detections into execution_log so governance
        # audits/evals can observe scanner security events in the main chain.
        if scan_result == "BLOCKED":
            _append_execution_log(session_id)

            # Also mirror to parent session for orchestrator-level observability.
            parent = conn.execute(
                "SELECT parent_session_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if parent and parent["parent_session_id"]:
                _append_execution_log(parent["parent_session_id"])
                _append_execution_log(parent["parent_session_id"], action_label="response_scan_mirror")
        conn.commit()
    finally:
        conn.close()

