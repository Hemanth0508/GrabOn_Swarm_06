"""
demo/scenarios/injector.py
Environmental pressure injection utilities.

Both functions are unconditional no-ops when ctx is None.
This preserves full backward compatibility — any caller that
passes ctx=None behaves identically to the pre-scenario runtime.

IMPORTANT:
  These functions are passive utilities. They fire only when explicitly
  called at lifecycle hook boundaries inside run_demo(). They do NOT
  monkey-patch, import-hook, or wrap any runtime function.

  maybe_fail()     → probabilistic TimeoutError at tool execution boundaries
  apply_latency()  → simulated operational latency around event execution

The runtime has no knowledge these functions exist.
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import ScenarioContext


# ── Latency map ────────────────────────────────────────────────────────────────
# Seconds of sleep per latency tier.
# LOW = 0 so baseline runs incur zero overhead.

_LATENCY_SECONDS: dict[str, float] = {
    "LOW":    0.0,
    "MEDIUM": 0.1,
    "HIGH":   0.3,
}


# ── Injection functions ────────────────────────────────────────────────────────

def maybe_fail(ctx, tool: str, action: str) -> None:
    """
    Probabilistically raise TimeoutError to simulate tool-level instability.

    Uses ctx.rng (isolated Random instance) — never touches global random state.
    Increments ctx.metrics.tool_timeouts_injected on every injection.

    Args:
        ctx:    ScenarioContext or None. No-op when None.
        tool:   Name of the tool being called (for future filtering).
        action: Action being performed (for future filtering).

    Raises:
        TimeoutError: When the RNG roll falls below tool_failure_rate.
    """
    if ctx is None:
        return

    if ctx.rng.random() < ctx.scenario.tool_failure_rate:
        ctx.metrics.tool_timeouts_injected += 1
        raise TimeoutError(
            f"[injector] Simulated tool timeout: {tool}/{action} "
            f"(scenario={ctx.scenario.name}, "
            f"rate={ctx.scenario.tool_failure_rate})"
        )


def apply_latency(ctx) -> None:
    """
    Sleep for the duration associated with ctx.scenario.latency.

    LOW    → 0s   (no sleep call made)
    MEDIUM → 0.1s
    HIGH   → 0.3s

    Args:
        ctx: ScenarioContext or None. No-op when None.
    """
    if ctx is None:
        return

    duration = _LATENCY_SECONDS.get(ctx.scenario.latency, 0.0)
    if duration > 0.0:
        time.sleep(duration)