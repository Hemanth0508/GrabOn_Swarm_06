"""
runtime/interceptor/validate.py
Agent Governance v2 — Core Enforcement Interceptor

The sole enforcement function. Every agent tool call passes through here.
No tool is reachable without passing all checks in order.

The interceptor is stateless in its own logic — all state is read fresh
from the state store on every call. Agent assertions about identity or
constraint state are irrelevant. The interceptor reads and decides
independently.

Checks in fixed order. First failure blocks immediately.
Order MUST NOT change.

Check 1 — Session existence
Check 2 — Session validity (active, not expired, not frozen for new actions)
Check 3 — Identity continuity (principal + type match session record)
Check 4 — Constraint version freshness (read-your-writes consistency)
Check 5 — Re-authentication gate
Check 6 — Dynamic constraint evaluation (taint, budget, capability)
Check 6.5 — Rate limiting (NEW — per tool per session per window)
Check 6.6 — Loop detection (NEW — same tool+action blocked 3x → ESCALATE)
Check 7 — Idempotency (deduplication on retry)

Outcomes:
    ALLOWED  — all checks pass, triggers executed, log written
    BLOCKED  — a check failed, tool never contacted, log written
    PENDING  — action requires human approval, approval record written
    ESCALATE — loop detected, Orchestrator notified, log written (NEW)
"""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from runtime.schema import get_connection
from runtime.constraints.store import (
    _budget_lock,
    get_constraint,
    get_constraint_version,
    write_constraint,
    CONSTRAINT_DEFAULTS,
    INTERCEPTOR_PRINCIPAL_ID,
)

# ── Token signing secret ─────────────────────────────────────────────────────
import os as _os
_TOKEN_SECRET = _os.environ.get('GOVERNANCE_TOKEN_SECRET', 'dev-secret-change-in-production').encode()

# ── Sensitive actions — always require reauth_verified = True ────────────────
SENSITIVE_ACTIONS: set[str] = {
    "access_sensitive",
}

# ── Exfiltration path actions — blocked after PII taint ──────────────────────
PII_TAINT_BLOCKED_ACTIONS: set[str] = {
    "post_message",
    "send_email",
    "post_slack",
    "post_external",
}

# ── Actions that always escalate to PENDING ──────────────────────────────────
ALWAYS_ESCALATE: set[str] = set()

# ── Budget spend actions ──────────────────────────────────────────────────────
BUDGET_ACTIONS: set[str] = {
    "process_payment",
    "spend_budget",
}

# ── Tool → capability mapping ─────────────────────────────────────────────────
CAPABILITY_REQUIREMENTS: dict[tuple[str, str], str] = {
    ("database",       "query_records"):     "can_query_records",
    ("database",       "query_pii_table"):   "can_query_pii",
    ("slack_api",      "post_message"):      "can_post_external",
    ("email_api",      "send_email"):        "can_post_external",
    ("budget_spend",   "process_payment"):   "can_spend_budget",
    ("sensitive_data", "access_sensitive"):  "can_access_sensitive",
    ("reauth_check",   "valid_credentials"): "can_reauth",
    ("session_mgmt",   "spawn_child"):       "can_spawn",
}

# ── Trigger map — on ALLOWED, which constraints to write ─────────────────────
def _trigger_pii_taint(session_id, principal_id, metadata):
    write_constraint(
        session_id=session_id,
        key="pii_accessed",
        value=True,
        written_by_principal=INTERCEPTOR_PRINCIPAL_ID,
        principal_type="INTERCEPTOR",
    )

def _trigger_reauth(session_id, principal_id, metadata):
    valid_until = (
        datetime.now(timezone.utc) + timedelta(minutes=15)
    ).isoformat()
    write_constraint(
        session_id=session_id,
        key="reauth_verified",
        value=True,
        written_by_principal=INTERCEPTOR_PRINCIPAL_ID,
        principal_type="INTERCEPTOR",
        valid_until=valid_until,
    )

TRIGGER_MAP: dict[tuple[str, str], Any] = {
    ("database",     "query_pii_table"):    _trigger_pii_taint,
    ("reauth_check", "valid_credentials"):  _trigger_reauth,
}

# ── PENDING escalation thresholds ────────────────────────────────────────────
PENDING_APPROVAL_TIMEOUT_SECONDS = 300
PENDING_BUDGET_THRESHOLD = 200.0

# ── Idempotency key TTL ───────────────────────────────────────────────────────
IDEMPOTENCY_KEY_TTL_SECONDS = 3600

# ── Principal types that must sign constraint writes ─────────────────────────
SIGNED_WRITE_REQUIRED_TYPES: set[str] = {'HUMAN', 'ORCHESTRATOR'}

# ── Actions treated as critical for failure-mode handling ────────────────────
CRITICAL_ACTIONS: set[str] = BUDGET_ACTIONS | SENSITIVE_ACTIONS

# ── Rate limiting config (NEW) ───────────────────────────────────────────────
# 10 calls per tool per 60-second window per session.
# Set below Gemini free tier (15/min) so governance trips first.
RATE_LIMIT_MAX    = 10   # max calls per window
RATE_LIMIT_WINDOW = 60   # seconds

# ── Loop detection config (NEW) ──────────────────────────────────────────────
# If the same tool+action is BLOCKED N times within the window → ESCALATE.
LOOP_DETECT_THRESHOLD = 3    # consecutive blocks to trigger ESCALATE
LOOP_DETECT_WINDOW    = 300  # 5-minute lookback window in seconds


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InterceptorDecision:
    """
    Result of every validate() call.

    Fields:
        allowed:    True if ALLOWED, False otherwise.
        result:     Literal string: 'ALLOWED', 'BLOCKED', 'PENDING', or 'ESCALATE'.
        reason:     Human-readable explanation of the decision.
        tool:       Tool name from the request.
        action:     Action name from the request.
        timestamp:  ISO8601 UTC timestamp of the decision.
        action_id:  UUID of this decision.
        constraint_version_at_decision: State store version at decision time.
        approval_id: UUID of the pending_approvals record (PENDING only).
        escalation_reason: Structured reason string (ESCALATE only).
    """
    allowed:    bool
    result:     str
    reason:     str
    tool:       str
    action:     str
    timestamp:  str
    action_id:  str
    constraint_version_at_decision: int
    approval_id: Optional[str] = field(default=None)
    capability_token: Optional[str] = field(default=None)
    scanner_required: bool = field(default=True)
    escalation_reason: Optional[str] = field(default=None)  # NEW

    def __str__(self) -> str:
        return (
            f"[{self.result}] {self.tool}/{self.action} "
            f"— {self.reason} "
            f"(v{self.constraint_version_at_decision})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting helpers (NEW)
# ─────────────────────────────────────────────────────────────────────────────

def _get_window_start(window_seconds: int = RATE_LIMIT_WINDOW) -> str:
    """Return ISO8601 timestamp of the start of the current rolling window."""
    now = datetime.now(timezone.utc)
    # Align to window boundary so all calls in same window share same key
    window_epoch = int(now.timestamp()) // window_seconds * window_seconds
    return datetime.fromtimestamp(window_epoch, tz=timezone.utc).isoformat()


def _check_rate_limit(conn, session_id: str, tool: str) -> tuple[bool, str]:
    """
    Check if this session has exceeded the rate limit for this tool.

    Returns (True, "") if within limit.
    Returns (False, reason) if limit exceeded.

    Uses INSERT OR IGNORE + UPDATE to atomically increment the counter
    within the current window. Old windows are purged on every write.
    """
    now_iso      = datetime.now(timezone.utc).isoformat()
    window_start = _get_window_start()

    # Purge expired windows for this session+tool before reading
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW * 2)
    ).isoformat()
    try:
        with conn:
            conn.execute(
                """
                DELETE FROM rate_limit_counters
                WHERE session_id = ? AND tool = ? AND window_start < ?
                """,
                (session_id, tool, cutoff),
            )
    except Exception:
        pass  # Purge failure must not affect enforcement

    # Enforce against a true rolling window so threshold behavior is deterministic
    # across wall-clock boundary crossings.
    rolling_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW)
    ).isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(call_count), 0) AS cnt
        FROM rate_limit_counters
        WHERE session_id = ? AND tool = ? AND last_updated > ?
        """,
        (session_id, tool, rolling_cutoff),
    ).fetchone()

    current_count = row["cnt"] if row else 0

    if current_count >= RATE_LIMIT_MAX:
        return False, (
            f"rate_limit_exceeded: {tool} called {current_count} times "
            f"in {RATE_LIMIT_WINDOW}s window (max={RATE_LIMIT_MAX})"
        )

    return True, ""


def _increment_rate_counter(conn, session_id: str, tool: str) -> None:
    """
    Increment the rate limit counter for this session+tool in the current window.
    Called after an ALLOWED decision is finalized.
    """
    now_iso      = datetime.now(timezone.utc).isoformat()
    window_start = _get_window_start()

    try:
        with conn:
            conn.execute(
                """
                INSERT INTO rate_limit_counters
                    (session_id, tool, window_start, call_count, last_updated)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(session_id, tool, window_start)
                DO UPDATE SET
                    call_count   = call_count + 1,
                    last_updated = excluded.last_updated
                """,
                (session_id, tool, window_start, now_iso),
            )
    except Exception:
        pass  # Counter increment failure must not affect enforcement outcome


# ─────────────────────────────────────────────────────────────────────────────
# Loop detection helpers (NEW)
# ─────────────────────────────────────────────────────────────────────────────

def _check_loop_detection(conn, session_id: str, tool: str, action: str) -> tuple[bool, str]:
    """
    Detect if the same tool+action has been BLOCKED repeatedly.

    Returns (False, escalation_reason) if loop detected.
    Returns (True, "") if no loop detected.

    Logic: count BLOCKED results for this (session_id, tool, action)
    in the last LOOP_DETECT_WINDOW seconds. If count >= LOOP_DETECT_THRESHOLD
    → ESCALATE. This catches retry loops without caring about intent —
    progress (or lack thereof) is the signal, not the reason for blocking.
    """
    window_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=LOOP_DETECT_WINDOW)
    ).isoformat()

    row = conn.execute(
        """
        SELECT COUNT(*) as cnt FROM execution_log
        WHERE session_id = ?
          AND tool = ?
          AND action = ?
          AND result = 'BLOCKED'
          AND timestamp > ?
        """,
        (session_id, tool, action, window_cutoff),
    ).fetchone()

    block_count = row["cnt"] if row else 0

    if block_count >= LOOP_DETECT_THRESHOLD:
        reason = (
            f"loop_detected: {tool}/{action} blocked {block_count} times "
            f"in {LOOP_DETECT_WINDOW}s — escalating to Orchestrator"
        )
        return False, reason

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Main enforcement function
# ─────────────────────────────────────────────────────────────────────────────

def _validate_inner(
    session_id:               str,
    claimed_principal:        str,
    claimed_principal_type:   str,
    tool:                     str,
    action:                   str,
    metadata:                 dict = None,
    idempotency_key:          Optional[str] = None,
    caller_constraint_version: int = 0,
    parent_action_id:         Optional[str] = None,
) -> InterceptorDecision:
    """
    The single enforcement function. Runs all checks in order.

    Every agent tool call must pass through this function. No check is
    skipped. No shortcut is taken. First failing check returns BLOCK
    immediately without evaluating remaining checks.

    On ALLOWED:
        - Execute trigger map
        - Write budget_spent inside linearizable lock (for budget actions)
        - Write idempotency key
        - Increment rate limit counter
        - Write execution_log with hash chain

    On BLOCKED:
        - Write execution_log only
        - Tool is NEVER contacted

    On PENDING:
        - Write pending_approvals record
        - Write execution_log with result=PENDING

    On ESCALATE:
        - Loop detected — same tool+action blocked >= LOOP_DETECT_THRESHOLD times
        - Write execution_log with result=ESCALATE
        - Orchestrator receives escalation_reason field
        - Tool is NEVER contacted
    """
    if metadata is None:
        metadata = {}

    timestamp  = datetime.now(timezone.utc).isoformat()
    action_id  = str(uuid.uuid4())
    conn       = get_connection()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _block(reason: str, version: int = 0, skip_log: bool = False) -> InterceptorDecision:
        # Loop-detection escalation must also run on early BLOCK returns.
        # Without this, repeated failures in checks 1-6 never reach CHECK 6.6.
        if not skip_log:
            try:
                window_cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=LOOP_DETECT_WINDOW)
                ).isoformat()
                row = conn.execute(
                    """
                    SELECT COUNT(*) as cnt FROM execution_log
                    WHERE session_id = ?
                      AND tool = ?
                      AND action = ?
                      AND result = 'BLOCKED'
                      AND timestamp > ?
                    """,
                    (session_id, tool, action, window_cutoff),
                ).fetchone()
                prior_block_count = row["cnt"] if row else 0

                if prior_block_count >= LOOP_DETECT_THRESHOLD:
                    esc_reason = (
                        f"loop_detected: {tool}/{action} blocked {prior_block_count} times "
                        f"in {LOOP_DETECT_WINDOW}s — escalating to Orchestrator"
                    )
                    decision = InterceptorDecision(
                        allowed=False,
                        result="ESCALATE",
                        reason=esc_reason,
                        tool=tool,
                        action=action,
                        timestamp=timestamp,
                        action_id=action_id,
                        constraint_version_at_decision=version,
                        escalation_reason=esc_reason,
                    )
                    try:
                        _write_log(conn, decision, session_id, claimed_principal, parent_action_id)
                    except Exception:
                        pass
                    conn.close()
                    return decision
            except Exception:
                pass

        decision = InterceptorDecision(
            allowed=False,
            result="BLOCKED",
            reason=reason,
            tool=tool,
            action=action,
            timestamp=timestamp,
            action_id=action_id,
            constraint_version_at_decision=version,
        )
        if not skip_log:
            try:
                _write_log(conn, decision, session_id, claimed_principal, parent_action_id)
                if "rate_limit_exceeded" in reason:
                    parent = conn.execute(
                        """
                        SELECT parent_session_id FROM sessions
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    if parent and parent["parent_session_id"]:
                        pinfo = conn.execute(
                            """
                            SELECT principal_id FROM sessions
                            WHERE session_id = ?
                            """,
                            (parent["parent_session_id"],),
                        ).fetchone()
                        if pinfo:
                            mirror = InterceptorDecision(
                                allowed=False,
                                result="BLOCKED",
                                reason=reason,
                                tool=tool,
                                action=action,
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                action_id=str(uuid.uuid4()),
                                constraint_version_at_decision=version,
                            )
                            _write_log(
                                conn,
                                mirror,
                                parent["parent_session_id"],
                                pinfo["principal_id"],
                                decision.action_id,
                            )
            except Exception:
                pass
        conn.close()
        return decision

    def _allow(reason: str, version: int) -> InterceptorDecision:
        decision = InterceptorDecision(
            allowed=True,
            result="ALLOWED",
            reason=reason,
            tool=tool,
            action=action,
            timestamp=timestamp,
            action_id=action_id,
            constraint_version_at_decision=version,
            scanner_required=True,
        )
        _write_log(conn, decision, session_id, claimed_principal, parent_action_id)
        conn.close()
        return decision

    def _pending(reason: str, version: int, approval_id: str) -> InterceptorDecision:
        decision = InterceptorDecision(
            allowed=False,
            result="PENDING",
            reason=reason,
            tool=tool,
            action=action,
            timestamp=timestamp,
            action_id=action_id,
            constraint_version_at_decision=version,
            approval_id=approval_id,
        )
        _write_log(conn, decision, session_id, claimed_principal, parent_action_id)
        conn.close()
        return decision

    def _escalate(reason: str, version: int) -> InterceptorDecision:
        """
        NEW — ESCALATE outcome.
        Fired when loop detection triggers. Tool is never contacted.
        Orchestrator reads escalation_reason to decide recovery action.
        """
        decision = InterceptorDecision(
            allowed=False,
            result="ESCALATE",
            reason=reason,
            tool=tool,
            action=action,
            timestamp=timestamp,
            action_id=action_id,
            constraint_version_at_decision=version,
            escalation_reason=reason,
        )
        try:
            _write_log(conn, decision, session_id, claimed_principal, parent_action_id)
        except Exception:
            pass
        conn.close()
        return decision

    # ═════════════════════════════════════════════════════════════════════════
    # Authentication (runs before all checks)
    # ═════════════════════════════════════════════════════════════════════════
    from runtime.identity.principals import verify_signature as _verify_sig

    if "signature" not in metadata:
        return _block("missing authentication signature", skip_log=True)

    _meta_for_auth = {k: v for k, v in metadata.items() if k != "signature"}
    _auth_message = f"{session_id}:{tool}:{action}:{json.dumps(_meta_for_auth, sort_keys=True)}".encode()
    if not _verify_sig(claimed_principal, _auth_message, metadata["signature"]):
        return _block("invalid authentication signature", skip_log=True)

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 1 — Session existence
    # ═════════════════════════════════════════════════════════════════════════
    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    if session is None:
        return _block("no active session found", skip_log=True)

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 2 — Session validity
    # ═════════════════════════════════════════════════════════════════════════
    if not session["active"]:
        state = session["state"]
        if state == "REVOKED":
            return _block("session has been revoked")
        return _block(f"session is not active (state={state})")

    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(session["expires_at"])
    if now > expires_at:
        return _block(f"session expired at {session['expires_at']}")

    if session["frozen"]:
        return _block("session is frozen — no new actions permitted")

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 3 — Identity continuity
    # ═════════════════════════════════════════════════════════════════════════
    if claimed_principal != session["principal_id"]:
        return _block(
            f"identity mismatch: claimed {claimed_principal}, "
            f"session bound to {session['principal_id']}"
        )

    if claimed_principal_type != session["principal_type"]:
        return _block(
            f"principal type mismatch: claimed {claimed_principal_type}, "
            f"session bound to {session['principal_type']}"
        )

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 4 — Constraint version freshness
    # ═════════════════════════════════════════════════════════════════════════
    current_version = get_constraint_version(session_id)

    parent_session_id = session["parent_session_id"]
    parent_version = 0
    if parent_session_id:
        parent_version = get_constraint_version(parent_session_id)

    effective_version = max(current_version, parent_version)

    if caller_constraint_version < effective_version:
        current_version = effective_version

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 5 — Re-authentication gate
    # ═════════════════════════════════════════════════════════════════════════
    reauth_verified = get_constraint(session_id, "reauth_verified")
    pii_accessed    = get_constraint(session_id, "pii_accessed")

    if not pii_accessed and parent_session_id:
        pii_accessed = get_constraint(parent_session_id, "pii_accessed")

    requires_reauth = action in SENSITIVE_ACTIONS
    if pii_accessed and action == "query_pii_table":
        requires_reauth = True

    if requires_reauth and not reauth_verified:
        return _block(
            "re-authentication required for sensitive action",
            version=current_version,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 6 — Dynamic constraint evaluation
    # ═════════════════════════════════════════════════════════════════════════

    # ── 6a. PII taint exfiltration block ─────────────────────────────────────
    if pii_accessed and action in PII_TAINT_BLOCKED_ACTIONS:
        return _block(
            f"pii_accessed constraint: {tool}/{action} blocked as potential "
            f"exfiltration path after PII access this session",
            version=current_version,
        )

    # ── 6b. Budget enforcement ────────────────────────────────────────────────
    if action in BUDGET_ACTIONS:
        amount = metadata.get("amount", 0.0)

        if not isinstance(amount, (int, float)) or amount <= 0:
            return _block(
                f"invalid spend amount: {amount!r} — must be a positive number",
                version=current_version,
            )

        with _budget_lock:
            budget_limit = get_constraint(session_id, "budget_limit")
            budget_spent = get_constraint(session_id, "budget_spent")

            if budget_limit is None:
                budget_limit = session["budget_grant"]

            if budget_spent is None:
                budget_spent = 0.0

            if budget_spent + amount > budget_limit:
                return _block(
                    f"budget exceeded: limit={budget_limit:.2f}, "
                    f"spent={budget_spent:.2f}, "
                    f"requested={amount:.2f}",
                    version=current_version,
                )

            if amount > PENDING_BUDGET_THRESHOLD or action in ALWAYS_ESCALATE:
                pending_decision = InterceptorDecision(
                    allowed=False, result="PENDING",
                    reason=f"spend amount {amount:.2f} exceeds approval threshold "
                           f"{PENDING_BUDGET_THRESHOLD:.2f} — awaiting human approval",
                    tool=tool, action=action, timestamp=timestamp,
                    action_id=action_id, constraint_version_at_decision=current_version,
                )
                _write_log(conn, pending_decision, session_id, claimed_principal, parent_action_id)
                approval_id = _write_pending_approval(
                    conn, action_id, session_id, tool, action, metadata
                )
                pending_decision.approval_id = approval_id
                conn.close()
                return pending_decision

            _ledger_row = conn.execute(
                'SELECT current_value FROM principal_ledger '
                'WHERE principal_id = ? AND metric_key = ?',
                (claimed_principal, 'budget_spent'),
            ).fetchone()
            _ledger_spent = _ledger_row['current_value'] if _ledger_row else 0.0

            _principal_limit = get_constraint(session_id, 'principal_budget_limit')
            if _principal_limit is not None:
                if _ledger_spent + amount > _principal_limit:
                    return _block(
                        f'principal budget limit exceeded: '
                        f'cross-session spent={_ledger_spent:.2f}, '
                        f'requested={amount:.2f}, '
                        f'principal_limit={_principal_limit:.2f}',
                        version=current_version,
                    )

            new_spent = budget_spent + amount
            write_constraint(
                session_id=session_id,
                key="budget_spent",
                value=new_spent,
                written_by_principal=INTERCEPTOR_PRINCIPAL_ID,
                principal_type="INTERCEPTOR",
            )

            _update_principal_ledger(conn, claimed_principal, amount)

    # ── 6c. Capability check ──────────────────────────────────────────────────
    required_cap = CAPABILITY_REQUIREMENTS.get((tool, action))
    if required_cap is not None:
        import json as _json
        granted_caps = set(_json.loads(session["granted_capabilities"]))
        if required_cap not in granted_caps:
            return _block(
                f"capability '{required_cap}' required for {tool}/{action} "
                f"but not granted in this session",
                version=current_version,
            )

    # ── PII + external PENDING escalation ────────────────────────────────────
    pii_and_external = (
        pii_accessed
        and action in PII_TAINT_BLOCKED_ACTIONS
    )
    if pii_and_external and not (pii_accessed and action in PII_TAINT_BLOCKED_ACTIONS):
        _pii_pending = InterceptorDecision(
            allowed=False, result="PENDING",
            reason=f"pii_accessed in session tree and {tool}/{action} is an exfiltration path — awaiting human approval",
            tool=tool, action=action, timestamp=timestamp,
            action_id=action_id, constraint_version_at_decision=current_version,
        )
        _write_log(conn, _pii_pending, session_id, claimed_principal, parent_action_id)
        _pii_approval_id = _write_pending_approval(conn, action_id, session_id, tool, action, metadata)
        _pii_pending.approval_id = _pii_approval_id
        conn.close()
        return _pii_pending

    if action in ALWAYS_ESCALATE:
        pending_decision = InterceptorDecision(
            allowed=False, result="PENDING",
            reason=f"{tool}/{action} is in the always-escalate set — awaiting human approval",
            tool=tool, action=action, timestamp=timestamp,
            action_id=action_id, constraint_version_at_decision=current_version,
        )
        _write_log(conn, pending_decision, session_id, claimed_principal, parent_action_id)
        approval_id = _write_pending_approval(conn, action_id, session_id, tool, action, metadata)
        pending_decision.approval_id = approval_id
        conn.close()
        return pending_decision

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 6.5 — Rate limiting (NEW)
    # Runs after all constraint checks but before idempotency.
    # Blocks calls that exceed RATE_LIMIT_MAX per tool per window.
    # ═════════════════════════════════════════════════════════════════════════
    rate_ok, rate_reason = _check_rate_limit(conn, session_id, tool)
    if not rate_ok:
        return _block(rate_reason, version=current_version)

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 6.6 — Loop detection (NEW)
    # Reads execution_log for repeated BLOCKED results on same tool+action.
    # If threshold reached → ESCALATE instead of BLOCKED.
    # ESCALATE is a system signal to the Orchestrator — not a user error.
    # ═════════════════════════════════════════════════════════════════════════
    loop_ok, loop_reason = _check_loop_detection(conn, session_id, tool, action)
    if not loop_ok:
        return _escalate(loop_reason, version=current_version)

    # ═════════════════════════════════════════════════════════════════════════
    # CHECK 7 — Idempotency
    # ═════════════════════════════════════════════════════════════════════════
    if idempotency_key:
        now_iso = datetime.now(timezone.utc).isoformat()
        cached = conn.execute(
            """
            SELECT result_cached, action_hash FROM idempotency_keys
            WHERE key = ? AND session_id = ? AND expires_at > ?
            """,
            (idempotency_key, session_id, now_iso),
        ).fetchone()

        if cached:
            _meta_for_hash = {k: v for k, v in metadata.items() if k != "signature"}
            _new_hash = hashlib.sha256(
                f"{session_id}:{tool}:{action}:{json.dumps(_meta_for_hash, sort_keys=True)}".encode()
            ).hexdigest()
            if cached["action_hash"] != _new_hash:
                return _block(
                    "idempotency_key_collision: same key submitted with different action content",
                    version=current_version,
                )

            cached_data = json.loads(cached["result_cached"])
            conn.close()

            _retry_expires = (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat()
            _retry_payload = f"{session_id}:{claimed_principal}:{tool}:{action}:{action_id}:{_retry_expires}"
            _retry_token = hmac.new(
                _TOKEN_SECRET, _retry_payload.encode(), hashlib.sha256
            ).hexdigest()

            _cached_decision = InterceptorDecision(
                allowed=cached_data.get("allowed", False),
                result=cached_data.get("result", "ALLOWED"),
                reason=f"[idempotent] {cached_data.get('reason', '')}",
                tool=tool,
                action=action,
                timestamp=timestamp,
                action_id=action_id,
                constraint_version_at_decision=current_version,
            )
            _cached_decision.capability_token = {
                "token":        _retry_token,
                "action_id":    action_id,
                "expires":      _retry_expires,
                "principal_id": claimed_principal,
            }
            return _cached_decision

        expires_at_iso = (
            datetime.now(timezone.utc) + timedelta(seconds=IDEMPOTENCY_KEY_TTL_SECONDS)
        ).isoformat()
        _meta_for_hash = {k: v for k, v in metadata.items() if k != "signature"}
        action_hash = hashlib.sha256(
            f"{session_id}:{tool}:{action}:{json.dumps(_meta_for_hash, sort_keys=True)}".encode()
        ).hexdigest()

        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO idempotency_keys
                        (key, session_id, action_hash, result_cached, first_seen, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        session_id,
                        action_hash,
                        json.dumps({"allowed": True, "result": "ALLOWED",
                                    "reason": "pending execution"}),
                        now_iso,
                        expires_at_iso,
                    ),
                )
                conn.execute(
                    "DELETE FROM idempotency_keys WHERE expires_at < ?",
                    (now_iso,),
                )
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════════════
    # ALL CHECKS PASSED — Execute trigger map and return ALLOWED
    # ═════════════════════════════════════════════════════════════════════════

    if action not in BUDGET_ACTIONS:
        trigger_fn = TRIGGER_MAP.get((tool, action))
        if trigger_fn:
            try:
                trigger_fn(session_id, claimed_principal, metadata)
            except Exception:
                pass

    if idempotency_key:
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET result_cached = ?
                    WHERE key = ?
                    """,
                    (
                        json.dumps({
                            "allowed": True,
                            "result": "ALLOWED",
                            "reason": "identity verified, no constraints violated",
                        }),
                        idempotency_key,
                    ),
                )
        except Exception:
            pass

    # Generate capability token
    _token_expires = (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    ).isoformat()
    _token_payload = f"{session_id}:{claimed_principal}:{tool}:{action}:{action_id}:{_token_expires}"
    _capability_token = hmac.new(
        _TOKEN_SECRET,
        _token_payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    # Increment rate limit counter on ALLOWED (NEW)
    _increment_rate_counter(conn, session_id, tool)

    decision = _allow(
        "identity verified, no constraints violated",
        version=current_version,
    )
    decision.capability_token = {
        "token":        _capability_token,
        "action_id":    action_id,
        "expires":      _token_expires,
        "principal_id": claimed_principal,
    }

    return decision


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_log(
    conn,
    decision: InterceptorDecision,
    session_id: str,
    principal_id: str,
    parent_action_id: Optional[str],
) -> None:
    """
    Append a decision record to the execution_log with hash chaining.
    Supports ALLOWED, BLOCKED, PENDING, and ESCALATE results.
    """
    prev_row = conn.execute(
        """
        SELECT entry_hash FROM execution_log
        WHERE session_id = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    prev_hash = prev_row["entry_hash"] if prev_row else "0" * 64

    chain_input = (
        f"{prev_hash}"
        f"{decision.action_id}"
        f"{decision.timestamp}"
        f"{decision.result}"
        f"{decision.reason}"
    ).encode("utf-8")
    entry_hash = hashlib.sha256(chain_input).hexdigest()

    with conn:
        conn.execute(
            """
            INSERT INTO execution_log (
                action_id, parent_action_id, session_id, principal_id,
                tool, action, result, reason,
                constraint_version, timestamp, prev_hash, entry_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.action_id,
                parent_action_id,
                session_id,
                principal_id,
                decision.tool,
                decision.action,
                decision.result,
                decision.reason,
                decision.constraint_version_at_decision,
                decision.timestamp,
                prev_hash,
                entry_hash,
            ),
        )


def validate(
    session_id:               str,
    claimed_principal:        str,
    claimed_principal_type:   str,
    tool:                     str,
    action:                   str,
    metadata:                 dict = None,
    idempotency_key:          Optional[str] = None,
    caller_constraint_version: int = 0,
    parent_action_id:         Optional[str] = None,
) -> InterceptorDecision:
    """
    Public entry point with failure-mode handling.

    Wraps _validate_inner() in try/except.
    Critical actions (budget, sensitive) BLOCK on unexpected error.
    Non-critical actions are ALLOWED in degraded mode.
    """
    try:
        return _validate_inner(
            session_id=session_id,
            claimed_principal=claimed_principal,
            claimed_principal_type=claimed_principal_type,
            tool=tool,
            action=action,
            metadata=metadata,
            idempotency_key=idempotency_key,
            caller_constraint_version=caller_constraint_version,
            parent_action_id=parent_action_id,
        )
    except Exception as exc:
        _ts  = datetime.now(timezone.utc).isoformat()
        _aid = str(uuid.uuid4())
        if action in CRITICAL_ACTIONS:
            return InterceptorDecision(
                allowed=False,
                result='BLOCKED',
                reason=f'state_store_unavailable: {type(exc).__name__}',
                tool=tool,
                action=action,
                timestamp=_ts,
                action_id=_aid,
                constraint_version_at_decision=0,
            )
        return InterceptorDecision(
            allowed=True,
            result='ALLOWED',
            reason='degraded_mode: state store unavailable, non-critical action allowed',
            tool=tool,
            action=action,
            timestamp=_ts,
            action_id=_aid,
            constraint_version_at_decision=0,
        )


def verify_capability_token(
    token: str,
    session_id: str,
    tool: str,
    action: str,
    action_id: str,
    token_expires: str,
    principal_id: str = "",
) -> bool:
    """
    Verify a capability token issued by the interceptor on an ALLOWED decision.
    """
    try:
        expires = datetime.fromisoformat(token_expires)
        if datetime.now(timezone.utc) > expires:
            return False
    except ValueError:
        return False

    _token_payload = f"{session_id}:{principal_id}:{tool}:{action}:{action_id}:{token_expires}"
    expected = hmac.new(
        _TOKEN_SECRET,
        _token_payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, token)


def _write_pending_approval(
    conn,
    action_id: str,
    session_id: str,
    tool: str,
    action: str,
    metadata: dict,
) -> str:
    approval_id  = str(uuid.uuid4())
    now_iso      = datetime.now(timezone.utc).isoformat()
    expires_at   = (
        datetime.now(timezone.utc) + timedelta(seconds=PENDING_APPROVAL_TIMEOUT_SECONDS)
    ).isoformat()

    with conn:
        conn.execute(
            """
            INSERT INTO pending_approvals (
                approval_id, action_id, session_id, tool, action,
                metadata, requested_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                approval_id,
                action_id,
                session_id,
                tool,
                action,
                json.dumps(metadata),
                now_iso,
                expires_at,
            ),
        )

    return approval_id


def _update_principal_ledger(conn, principal_id: str, amount: float) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()

    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO principal_ledger
                (principal_id, metric_key, current_value, last_updated, version)
            VALUES (?, 'budget_spent', 0.0, ?, 0)
            """,
            (principal_id, now_iso),
        )
        conn.execute(
            """
            UPDATE principal_ledger
            SET current_value = current_value + ?,
                last_updated  = ?,
                version       = version + 1
            WHERE principal_id = ? AND metric_key = 'budget_spent'
            """,
            (amount, now_iso, principal_id),
        )


def verify_audit_chain(session_id: str) -> tuple[bool, str]:
    """
    Verify the hash chain integrity of the execution_log for a session.
    """
    conn = get_connection()
    entries = conn.execute(
        """
        SELECT action_id, timestamp, result, reason, prev_hash, entry_hash
        FROM execution_log
        WHERE session_id = ?
        ORDER BY timestamp ASC
        """,
        (session_id,),
    ).fetchall()
    conn.close()

    if not entries:
        return True, "no entries — chain vacuously intact"

    for i, entry in enumerate(entries):
        expected_input = (
            f"{entry['prev_hash']}"
            f"{entry['action_id']}"
            f"{entry['timestamp']}"
            f"{entry['result']}"
            f"{entry['reason']}"
        ).encode("utf-8")
        expected_hash = hashlib.sha256(expected_input).hexdigest()

        if expected_hash != entry["entry_hash"]:
            return False, (
                f"chain broken at entry {i} "
                f"(action_id={entry['action_id']}): "
                f"expected hash {expected_hash[:16]}... "
                f"got {entry['entry_hash'][:16]}..."
            )

        if i + 1 < len(entries):
            next_entry = entries[i + 1]
            if next_entry["prev_hash"] != entry["entry_hash"]:
                return False, (
                    f"chain link broken between entry {i} and {i+1}: "
                    f"next.prev_hash={next_entry['prev_hash'][:16]}... "
                    f"but entry.hash={entry['entry_hash'][:16]}..."
                )

    return True, "chain intact"


def poll_pending_approval(approval_id: str) -> str:
    conn = get_connection()
    row = conn.execute(
        "SELECT status, expires_at FROM pending_approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return "NOT_FOUND"

    if row["status"] == "PENDING":
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(row["expires_at"])
        if now > expires:
            conn = get_connection()
            with conn:
                conn.execute(
                    """
                    UPDATE pending_approvals
                    SET status = 'TIMEOUT', decided_at = ?
                    WHERE approval_id = ?
                    """,
                    (now.isoformat(), approval_id),
                )
            conn.close()
            return "TIMEOUT"

    return row["status"]


def approve_pending(approval_id: str, decided_by_principal: str) -> bool:
    return _decide_pending(approval_id, "APPROVED", decided_by_principal)


def reject_pending(approval_id: str, decided_by_principal: str) -> bool:
    return _decide_pending(approval_id, "REJECTED", decided_by_principal)


def _decide_pending(approval_id: str, status: str, decided_by: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()

    if row is None or row["status"] != "PENDING":
        conn.close()
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            UPDATE pending_approvals
            SET status = ?, decided_at = ?, decided_by = ?
            WHERE approval_id = ?
            """,
            (status, now_iso, decided_by, approval_id),
        )
    conn.close()
    return True
