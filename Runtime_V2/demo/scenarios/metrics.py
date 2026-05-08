"""
demo/scenarios/metrics.py
Scenario run metrics.

Tracks real runtime outcomes — not synthetic counters.
All increments happen in _after_event() inside run_demo(), derived
from actual event_record data returned by the runtime.

No runtime imports. No side effects on construction.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from collections import Counter


@dataclass
class ScenarioMetrics:
    """
    Accumulated metrics for a single scenario run.

    All fields are incremented from real event outcomes in the lifecycle
    hooks inside run_demo(). Nothing here is synthetic or pre-filled.

    Fields:
        recovery_attempts          Events that raised an exception and were caught.
        escalations_triggered      Events whose result was "ESCALATE".
        budget_violations_prevented Events whose result was "BLOCKED" with a
                                   budget-related reason (derived from note field).
        tool_timeouts_injected     TimeoutErrors raised by injector.maybe_fail().
        resolution_paths           Ordered list of result strings across all events.
                                   Used to derive the dominant execution path.
    """
    recovery_attempts:          int        = 0
    escalations_triggered:      int        = 0
    budget_violations_prevented: int       = 0
    tool_timeouts_injected:     int        = 0
    resolution_paths:           list[str]  = field(default_factory=list)

    # ── Mutation helpers ───────────────────────────────────────────────────────

    def record_path(self, result: str) -> None:
        """Append a result token to the resolution path log."""
        self.resolution_paths.append(result)

    # ── Derived properties ─────────────────────────────────────────────────────

    def dominant_path(self) -> str:
        """
        Return the most frequently occurring result across all events.
        Returns "NONE" if no paths have been recorded yet.
        """
        if not self.resolution_paths:
            return "NONE"
        most_common = Counter(self.resolution_paths).most_common(1)
        return most_common[0][0]

    def as_display_pairs(self) -> list[tuple[str, str]]:
        """
        Return key/value pairs suitable for Rich display in the summary banner.
        Intentionally flat strings — no Rich markup — so the caller controls style.
        """
        return [
            ("Recovery attempts",           str(self.recovery_attempts)),
            ("Escalations triggered",        str(self.escalations_triggered)),
            ("Budget violations prevented",  str(self.budget_violations_prevented)),
            ("Tool timeouts injected",       str(self.tool_timeouts_injected)),
            ("Dominant resolution path",     self.dominant_path()),
        ]