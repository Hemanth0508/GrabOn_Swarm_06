"""
runtime/protocol/messages.py
Agent Governance v2 — Typed Inter-Agent Communication Protocol

Defines the formal message contracts for all inter-agent communication.
Every message exchanged between agents in the governed runtime must be
one of these types. No free-text dicts. No untyped payloads.

Philosophy:
    The runtime already enforced these semantics implicitly through
    governance events. The protocol layer formalises them as explicit
    inter-agent contracts.

    Agents propose. The runtime decides.
    A conflict resolution is only real once committed to the constraint store.
    Until then it is advisory metadata, not governed state.

Message types:
    Request             — agent requests an action or assessment from another
    Response            — agent returns result of a Request
    Escalation          — loop or failure detected, Orchestrator notified
    Approval            — human or Orchestrator approves a pending action
    Veto                — agent or Compliance rejects a proposed action/assessment
    RevisionNeeded      — Orchestrator disagrees with an assessment, requests re-evaluation
    HealthSignal        — heartbeat progress marker, stuck-agent detection
    ConstraintViolation — interceptor blocked an action, structured record of why

Runtime mapping:
    Escalation          ↔  GovernanceEscalate + ESCALATE in execution_log
    Veto                ↔  GovernanceBlock from constraint authority conflict
    Approval            ↔  PENDING → APPROVED path in pending_approvals
    HealthSignal        ↔  heartbeat_log is_stuck() result
    ConstraintViolation ↔  BLOCKED decision from interceptor
    RevisionNeeded      ↔  NEW — confidence-threshold-based conflict trigger

Conflict resolution rule:
    CONFIDENCE_THRESHOLD = 0.75
    analyst_confidence >= threshold   → analyst assessment wins
    orchestrator_confidence >= threshold → orchestrator revision wins
    both below threshold              → requires_tiebreaker=True → Compliance Agent
    Compliance verdict passes through interceptor → committed to constraint store
    Final state is cryptographically auditable via audit chain.

Dependencies:
    pydantic  — data validation and serialisation
    enum      — MessageType enum
    No runtime imports. This file is a pure contract definition.
    It has zero dependencies on schema, validate, store, or sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ── Confidence threshold for conflict resolution ──────────────────────────────
# If both sides are below this threshold, requires_tiebreaker fires automatically.
# Compliance Agent is the deterministic tiebreaker.
CONFIDENCE_THRESHOLD: float = 0.75


# ─────────────────────────────────────────────────────────────────────────────
# MessageType enum
# ─────────────────────────────────────────────────────────────────────────────

class MessageType(str, Enum):
    """
    All valid inter-agent message types in the governed runtime.
    Extends str so values serialise naturally to JSON without extra handling.
    """
    REQUEST              = "REQUEST"
    RESPONSE             = "RESPONSE"
    ESCALATION           = "ESCALATION"
    APPROVAL             = "APPROVAL"
    VETO                 = "VETO"
    REVISION_NEEDED      = "REVISION_NEEDED"
    HEALTH_SIGNAL        = "HEALTH_SIGNAL"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"


# ─────────────────────────────────────────────────────────────────────────────
# AgentMessage — base class
# ─────────────────────────────────────────────────────────────────────────────

class AgentMessage(BaseModel):
    """
    Base class for all inter-agent messages.

    Every message in the governed runtime carries these fields.
    Subclasses add type-specific payload fields.

    Fields:
        message_id:     UUID — unique per message instance.
        correlation_id: UUID — ties a request → response → escalation chain.
                        Set once on the originating Request, copied by all
                        downstream messages in the same workflow.
        sender_id:      principal_id of the sending agent.
        sender_type:    principal_type of the sending agent
                        (HUMAN, ORCHESTRATOR, AGENT, SUBAGENT, COMPLIANCE).
        target_id:      principal_id of the intended recipient.
        session_id:     Runtime session_id — links message to governance state.
        message_type:   MessageType enum — discriminator for routing.
        timestamp:      ISO8601 UTC timestamp of message creation.
    """
    message_id:     str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender_id:      str
    sender_type:    str
    target_id:      str
    session_id:     str
    message_type:   MessageType
    timestamp:      str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    model_config = {"frozen": False, "use_enum_values": True}

    def to_log_dict(self) -> dict:
        """
        Return a compact dict suitable for writing to the audit log or console.
        Truncates long fields for readability.
        """
        return {
            "message_id":     self.message_id[:8],
            "correlation_id": self.correlation_id[:8],
            "message_type":   self.message_type,
            "sender_id":      self.sender_id[:8],
            "sender_type":    self.sender_type,
            "target_id":      self.target_id[:8],
            "session_id":     self.session_id[:8],
            "timestamp":      self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Request
# ─────────────────────────────────────────────────────────────────────────────

class Request(AgentMessage):
    """
    An agent requests an action or assessment from another agent.

    GrabOn context:
        Data Agent (Crawler) requests Compliance Agent to assess
        merchant_risk for a flagged coupon source.

    Fields:
        action:      What is being requested (e.g. 'assess_merchant_risk').
        payload:     Typed dict carrying the request parameters.
        priority:    'LOW', 'NORMAL', 'HIGH', 'CRITICAL'.
        timeout_seconds: How long the sender will wait before escalating.
    """
    message_type:    MessageType = MessageType.REQUEST
    action:          str
    payload:         dict[str, Any] = Field(default_factory=dict)
    priority:        str = "NORMAL"
    timeout_seconds: int = 30


# ─────────────────────────────────────────────────────────────────────────────
# Response
# ─────────────────────────────────────────────────────────────────────────────

class Response(AgentMessage):
    """
    An agent returns the result of a Request.

    Fields:
        in_reply_to:  message_id of the originating Request.
        success:      True if the request was fulfilled.
        payload:      Result data. Empty dict on failure.
        confidence:   0.0-1.0. Confidence in the response.
                      Below CONFIDENCE_THRESHOLD → may trigger RevisionNeeded.
        error:        Error message if success=False.
    """
    message_type: MessageType = MessageType.RESPONSE
    in_reply_to:  str
    success:      bool
    payload:      dict[str, Any] = Field(default_factory=dict)
    confidence:   float = 1.0
    error:        Optional[str] = None

    @model_validator(mode="after")
    def confidence_range(self) -> "Response":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Escalation
# ─────────────────────────────────────────────────────────────────────────────

class Escalation(AgentMessage):
    """
    A loop or unrecoverable failure has been detected.
    Orchestrator is notified to decide recovery action.

    Maps to GovernanceEscalate + ESCALATE result in execution_log.

    GrabOn context:
        Data Agent has tried to scrape the same coupon endpoint 3 times
        and received the same 503 error. Loop detected. Orchestrator
        must decide: reassign, spawn fresh agent, or notify human.

    Fields:
        escalation_reason:   Structured reason from the interceptor.
        failed_tool:         Tool that triggered the loop.
        failed_action:       Action that triggered the loop.
        block_count:         How many times the action was blocked.
        recommended_action:  What the monitor recommends
                             ('reassign_to_sibling', 'retry_with_fresh_agent',
                              'check_tool_availability', 'notify_human').
        health_summary:      Optional dict from get_session_health().
    """
    message_type:       MessageType = MessageType.ESCALATION
    escalation_reason:  str
    failed_tool:        str
    failed_action:      str
    block_count:        int = 0
    recommended_action: str = "retry_with_fresh_agent"
    health_summary:     Optional[dict[str, Any]] = None


# ─────────────────────────────────────────────────────────────────────────────
# Approval
# ─────────────────────────────────────────────────────────────────────────────

class Approval(AgentMessage):
    """
    A human or Orchestrator approves a pending action.

    Maps to PENDING → APPROVED path in pending_approvals table.

    GrabOn context:
        Human approves a large budget spend (>$200) for a bulk
        coupon validation run during a flash sale.

    Fields:
        approval_id:   UUID from pending_approvals table.
        in_reply_to:   message_id of the originating Request or PENDING decision.
        approved_by:   principal_id of the approver.
        approved_by_type: principal_type of the approver.
        conditions:    Optional constraints on the approval
                       (e.g. 'max_spend=300', 'expires_in=600s').
    """
    message_type:      MessageType = MessageType.APPROVAL
    approval_id:       str
    in_reply_to:       str
    approved_by:       str
    approved_by_type:  str
    conditions:        dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Veto
# ─────────────────────────────────────────────────────────────────────────────

class Veto(AgentMessage):
    """
    An agent or Compliance rejects a proposed action or assessment.

    Maps to GovernanceBlock from constraint authority conflict,
    OR to Compliance Agent's final verdict in a conflict resolution.

    GrabOn context:
        Compliance Agent vetoes Orchestrator's proposed merchant_risk=MEDIUM
        after reviewing evidence. Final verdict: merchant_risk=HIGH.
        Veto passes through interceptor → committed to constraint store.

    Fields:
        in_reply_to:        message_id of the Request or RevisionNeeded being vetoed.
        vetoed_action:      What is being rejected.
        veto_reason:        Why it is being rejected.
        evidence:           Supporting data for the veto decision.
        final_verdict:      The authoritative assessment after veto.
                            This is what gets committed to the constraint store.
        constraint_key:     The constraint key to write to the store.
        constraint_value:   The constraint value to write to the store.
        requires_commitment: If True, final_verdict must pass through interceptor.
    """
    message_type:         MessageType = MessageType.VETO
    in_reply_to:          str
    vetoed_action:        str
    veto_reason:          str
    evidence:             dict[str, Any] = Field(default_factory=dict)
    final_verdict:        Optional[dict[str, Any]] = None
    constraint_key:       Optional[str] = None
    constraint_value:     Optional[Any] = None
    requires_commitment:  bool = True


# ─────────────────────────────────────────────────────────────────────────────
# RevisionNeeded
# ─────────────────────────────────────────────────────────────────────────────

class RevisionNeeded(AgentMessage):
    """
    Orchestrator disagrees with an agent's assessment and requests re-evaluation.

    Conflict resolution rule:
        analyst_confidence >= CONFIDENCE_THRESHOLD   → analyst wins
        orchestrator_confidence >= CONFIDENCE_THRESHOLD → orchestrator wins
        both below threshold                          → requires_tiebreaker=True
        requires_tiebreaker=True                     → Compliance Agent resolves

    GrabOn context:
        Data Agent flags merchant_risk=HIGH (confidence=0.60).
        Orchestrator proposes merchant_risk=MEDIUM (confidence=0.70).
        Both below 0.75 threshold → requires_tiebreaker=True.
        Compliance Agent reviews evidence and issues a Veto.
        Final verdict committed to constraint store via interceptor.

    Fields:
        in_reply_to:             message_id of the original Response being challenged.
        original_assessment:     The agent's original assessment dict.
        proposed_revision:       Orchestrator's proposed alternative.
        revision_reason:         Why the Orchestrator disagrees.
        analyst_confidence:      Confidence of the original assessment (0.0-1.0).
        orchestrator_confidence: Confidence of the proposed revision (0.0-1.0).
        requires_tiebreaker:     Auto-set True if both below CONFIDENCE_THRESHOLD.
        tiebreaker_agent_type:   Who resolves if tiebreaker needed. Default: COMPLIANCE.
    """
    message_type:            MessageType = MessageType.REVISION_NEEDED
    in_reply_to:             str
    original_assessment:     dict[str, Any]
    proposed_revision:       dict[str, Any]
    revision_reason:         str
    analyst_confidence:      float
    orchestrator_confidence: float
    requires_tiebreaker:     bool = False
    tiebreaker_agent_type:   str = "COMPLIANCE"

    @model_validator(mode="after")
    def auto_set_tiebreaker(self) -> "RevisionNeeded":
        """
        Automatically set requires_tiebreaker=True if both sides
        are below CONFIDENCE_THRESHOLD.
        """
        if not 0.0 <= self.analyst_confidence <= 1.0:
            raise ValueError(
                f"analyst_confidence must be 0.0-1.0, got {self.analyst_confidence}"
            )
        if not 0.0 <= self.orchestrator_confidence <= 1.0:
            raise ValueError(
                f"orchestrator_confidence must be 0.0-1.0, "
                f"got {self.orchestrator_confidence}"
            )
        both_below = (
            self.analyst_confidence      < CONFIDENCE_THRESHOLD and
            self.orchestrator_confidence < CONFIDENCE_THRESHOLD
        )
        if both_below:
            self.requires_tiebreaker = True
        return self

    def resolution_winner(self) -> str:
        """
        Determine who wins the conflict without a tiebreaker.
        Returns 'analyst', 'orchestrator', or 'tiebreaker_needed'.
        """
        if self.analyst_confidence >= CONFIDENCE_THRESHOLD:
            return "analyst"
        if self.orchestrator_confidence >= CONFIDENCE_THRESHOLD:
            return "orchestrator"
        return "tiebreaker_needed"


# ─────────────────────────────────────────────────────────────────────────────
# HealthSignal
# ─────────────────────────────────────────────────────────────────────────────

class HealthSignal(AgentMessage):
    """
    A heartbeat progress marker for stuck-agent detection.

    Maps to heartbeat_log is_stuck() result from runtime/monitor/heartbeat.py.
    Sent by the monitor when it detects abnormal progress patterns.

    GrabOn context:
        Heartbeat monitor detects Report Agent has received the same
        503 response 3 times — no forward progress on coupon validation.
        HealthSignal sent to Orchestrator with is_stuck=True.

    Fields:
        is_stuck:          True if agent is making no forward progress.
        stuck_tools:       List of (tool, action) pairs where stuck detected.
        total_heartbeats:  Total heartbeat entries for this session.
        last_activity:     ISO8601 timestamp of last ALLOWED tool call.
        health_status:     'OK', 'STUCK', 'LOOPING', or 'DEGRADED'.
        recommended_action: What the Orchestrator should do.
    """
    message_type:       MessageType = MessageType.HEALTH_SIGNAL
    is_stuck:           bool
    stuck_tools:        list[dict[str, str]] = Field(default_factory=list)
    total_heartbeats:   int = 0
    last_activity:      Optional[str] = None
    health_status:      str = "OK"
    recommended_action: str = "continue"


# ─────────────────────────────────────────────────────────────────────────────
# ConstraintViolation
# ─────────────────────────────────────────────────────────────────────────────

class ConstraintViolation(AgentMessage):
    """
    The interceptor blocked an action. Structured record of why.

    Maps to a BLOCKED decision from the interceptor (validate.py).
    Written when an agent attempts a tool call that violates a constraint.

    GrabOn context:
        Data Agent attempts to post validated coupon data to Slack
        after PII was accessed in the same session. Interceptor blocks.
        ConstraintViolation message created for the audit record.

    Fields:
        action_id:          UUID from execution_log — links to audit chain.
        blocked_tool:       Tool that was blocked.
        blocked_action:     Action that was blocked.
        violation_type:     Category of violation:
                            'pii_taint', 'budget_exceeded', 'capability_missing',
                            'session_expired', 'rate_limit_exceeded',
                            'identity_mismatch', 'reauth_required',
                            'loop_detected', 'tamper_detected'.
        violation_reason:   Full reason string from the interceptor.
        constraint_version: State store version at time of block.
        recoverable:        True if agent can recover without human intervention.
        recovery_hint:      What the agent should do to recover.
    """
    message_type:       MessageType = MessageType.CONSTRAINT_VIOLATION
    action_id:          str
    blocked_tool:       str
    blocked_action:     str
    violation_type:     str
    violation_reason:   str
    constraint_version: int = 0
    recoverable:        bool = True
    recovery_hint:      Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Factory helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_request(
    sender_id:      str,
    sender_type:    str,
    target_id:      str,
    session_id:     str,
    action:         str,
    payload:        dict[str, Any] = None,
    priority:       str = "NORMAL",
    timeout_seconds: int = 30,
) -> Request:
    """Create a new Request message with a fresh correlation_id."""
    return Request(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=target_id,
        session_id=session_id,
        action=action,
        payload=payload or {},
        priority=priority,
        timeout_seconds=timeout_seconds,
    )


def make_response(
    request:    Request,
    sender_id:  str,
    sender_type: str,
    success:    bool,
    payload:    dict[str, Any] = None,
    confidence: float = 1.0,
    error:      str = None,
) -> Response:
    """Create a Response that inherits the correlation_id from its Request."""
    return Response(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=request.sender_id,
        session_id=request.session_id,
        correlation_id=request.correlation_id,
        in_reply_to=request.message_id,
        success=success,
        payload=payload or {},
        confidence=confidence,
        error=error,
    )


def make_revision_needed(
    original_response:       Response,
    sender_id:               str,
    sender_type:             str,
    target_id:               str,
    proposed_revision:       dict[str, Any],
    revision_reason:         str,
    orchestrator_confidence: float,
) -> RevisionNeeded:
    """
    Create a RevisionNeeded from an existing Response.
    Inherits correlation_id. Auto-sets requires_tiebreaker via validator.
    """
    return RevisionNeeded(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=target_id,
        session_id=original_response.session_id,
        correlation_id=original_response.correlation_id,
        in_reply_to=original_response.message_id,
        original_assessment=original_response.payload,
        proposed_revision=proposed_revision,
        revision_reason=revision_reason,
        analyst_confidence=original_response.confidence,
        orchestrator_confidence=orchestrator_confidence,
    )


def make_veto(
    revision:         RevisionNeeded,
    sender_id:        str,
    sender_type:      str,
    vetoed_action:    str,
    veto_reason:      str,
    evidence:         dict[str, Any] = None,
    final_verdict:    dict[str, Any] = None,
    constraint_key:   str = None,
    constraint_value: Any = None,
) -> Veto:
    """
    Create a Veto from a RevisionNeeded.
    Inherits correlation_id. requires_commitment=True by default.
    Final verdict must be committed to constraint store via interceptor.
    """
    return Veto(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=revision.sender_id,
        session_id=revision.session_id,
        correlation_id=revision.correlation_id,
        in_reply_to=revision.message_id,
        vetoed_action=vetoed_action,
        veto_reason=veto_reason,
        evidence=evidence or {},
        final_verdict=final_verdict,
        constraint_key=constraint_key,
        constraint_value=constraint_value,
        requires_commitment=True,
    )


def make_health_signal(
    sender_id:    str,
    sender_type:  str,
    target_id:    str,
    session_id:   str,
    progress:     dict[str, Any],
    health:       dict[str, Any],
    correlation_id: str = None,
) -> HealthSignal:
    """
    Create a HealthSignal from heartbeat progress + live health summaries.
    progress = output of get_progress_summary()
    health   = output of get_session_health()
    """
    return HealthSignal(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=target_id,
        session_id=session_id,
        correlation_id=correlation_id or str(uuid.uuid4()),
        is_stuck=bool(progress.get("stuck_tools")),
        stuck_tools=progress.get("stuck_tools", []),
        total_heartbeats=progress.get("total_heartbeats", 0),
        last_activity=progress.get("last_activity"),
        health_status=health.get("health", "OK"),
        recommended_action=health.get("recommended_action", "continue"),
    )


def make_constraint_violation(
    sender_id:    str,
    sender_type:  str,
    target_id:    str,
    session_id:   str,
    decision,                    # InterceptorDecision
    recoverable:  bool = True,
    recovery_hint: str = None,
    correlation_id: str = None,
) -> ConstraintViolation:
    """
    Create a ConstraintViolation from an InterceptorDecision (BLOCKED result).
    """
    violation_type = "blocked"
    reason = decision.reason.lower()
    if "pii_accessed"    in reason: violation_type = "pii_taint"
    elif "budget"        in reason: violation_type = "budget_exceeded"
    elif "capability"    in reason: violation_type = "capability_missing"
    elif "expired"       in reason: violation_type = "session_expired"
    elif "rate_limit"    in reason: violation_type = "rate_limit_exceeded"
    elif "identity"      in reason: violation_type = "identity_mismatch"
    elif "reauth"        in reason: violation_type = "reauth_required"
    elif "loop_detected" in reason: violation_type = "loop_detected"
    elif "tamper"        in reason: violation_type = "tamper_detected"

    return ConstraintViolation(
        sender_id=sender_id,
        sender_type=sender_type,
        target_id=target_id,
        session_id=session_id,
        correlation_id=correlation_id or str(uuid.uuid4()),
        action_id=decision.action_id,
        blocked_tool=decision.tool,
        blocked_action=decision.action,
        violation_type=violation_type,
        violation_reason=decision.reason,
        constraint_version=decision.constraint_version_at_decision,
        recoverable=recoverable,
        recovery_hint=recovery_hint,
    )