"""
demo/scenarios/profiles.py
Declarative scenario profiles for the governed runtime simulation framework.

Rules:
  - This file is configuration only. No runtime logic lives here.
  - No imports from runtime, demo, or injector modules.
  - Scenario instances are immutable value objects.
  - The runtime has no knowledge this file exists.

Usage:
    from demo.scenarios.profiles import SCENARIOS
    scenario = SCENARIOS["TOOL_INSTABILITY"]
"""

from dataclasses import dataclass


# ── Runtime config key constants ───────────────────────────────────────────────
# Centralised so downstream consumers (engine, runner, CI) never use magic strings.

RC_BUDGET_LIMIT         = "budget_limit"
RC_APPROVAL_THRESHOLD   = "approval_threshold"
RC_ESCALATION_THRESHOLD = "escalation_threshold"
RC_PROTOCOL             = "protocol"
RC_RETRY_BUDGET         = "retry_budget"


# ── Scenario dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Scenario:
    """
    Immutable scenario profile.

    Fields:
        name                 Unique identifier. Also used as RNG seed.
        budget_limit         Runtime budget cap passed via runtime_config.
        approval_threshold   Spend level that triggers PENDING approval.
        tool_failure_rate    0.0–1.0 probability a tool call raises TimeoutError.
        latency              "LOW" / "MEDIUM" / "HIGH" — simulated op latency.
        retry_budget         Max retries before ESCALATE is triggered externally.
        escalation_threshold Confidence threshold for protocol conflict resolution.
        protocol             "STRICT" / "LENIENT" — arbitration mode.
    """
    name:                 str
    budget_limit:         float
    approval_threshold:   float
    tool_failure_rate:    float
    latency:              str
    retry_budget:         int
    escalation_threshold: float
    protocol:             str


# ── Predefined scenario registry ──────────────────────────────────────────────

SCENARIOS: dict[str, Scenario] = {

    # ── BASELINE ──────────────────────────────────────────────────────────────
    # Mirrors the current default runtime parameters exactly.
    # No injected failures. No latency pressure.
    # Used as the reference run for comparison and regression.
    "BASELINE": Scenario(
        name                 = "BASELINE",
        budget_limit         = 500.0,
        approval_threshold   = 250.0,
        tool_failure_rate    = 0.0,
        latency              = "LOW",
        retry_budget         = 3,
        escalation_threshold = 0.7,
        protocol             = "LENIENT",
    ),

    # ── BUDGET_PRESSURE ───────────────────────────────────────────────────────
    # Tight budget cap (150) with a low approval threshold (75).
    # Designed to force early PENDING and BLOCKED states, exercising the
    # budget enforcement and approval-gate governance paths.
    "BUDGET_PRESSURE": Scenario(
        name                 = "BUDGET_PRESSURE",
        budget_limit         = 150.0,
        approval_threshold   = 75.0,
        tool_failure_rate    = 0.0,
        latency              = "LOW",
        retry_budget         = 3,
        escalation_threshold = 0.7,
        protocol             = "STRICT",
    ),

    # ── TOOL_INSTABILITY ──────────────────────────────────────────────────────
    # 40% tool failure rate with high latency.
    # Designed to exercise recovery paths, retry turbulence,
    # and escalation behavior under degraded tool conditions.
    "TOOL_INSTABILITY": Scenario(
        name                 = "TOOL_INSTABILITY",
        budget_limit         = 500.0,
        approval_threshold   = 250.0,
        tool_failure_rate    = 0.4,
        latency              = "HIGH",
        retry_budget         = 2,
        escalation_threshold = 0.7,
        protocol             = "STRICT",
    ),

    # ── PROTOCOL_CONFLICT ─────────────────────────────────────────────────────
    # High escalation threshold (0.9) with STRICT protocol.
    # Designed to trigger tiebreaker and compliance arbitration flows
    # more frequently by raising the bar for uncontested resolution.
    "PROTOCOL_CONFLICT": Scenario(
        name                 = "PROTOCOL_CONFLICT",
        budget_limit         = 500.0,
        approval_threshold   = 250.0,
        tool_failure_rate    = 0.1,
        latency              = "MEDIUM",
        retry_budget         = 3,
        escalation_threshold = 0.9,
        protocol             = "STRICT",
    ),
}