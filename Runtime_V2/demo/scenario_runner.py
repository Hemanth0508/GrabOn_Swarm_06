"""
demo/scenario_runner.py
Scenario Runner — governed runtime experimentation entry point.

Usage:
    python -m demo.scenario_runner --scenario TOOL_INSTABILITY
    python -m demo.scenario_runner --scenario BUDGET_PRESSURE
    python -m demo.scenario_runner --scenario BASELINE
    python -m demo.scenario_runner --scenario PROTOCOL_CONFLICT
    python -m demo.scenario_runner           # defaults to BASELINE

    # Pass-through flags to the underlying demo:
    python -m demo.scenario_runner --scenario TOOL_INSTABILITY --event 8
    python -m demo.scenario_runner --scenario BASELINE --summary

Architecture:
    This file is a thin harness shell. It:
      1. Resolves the named scenario from profiles.SCENARIOS
      2. Builds a ScenarioContext (isolated RNG, fresh metrics)
      3. Translates the scenario into a generic runtime_config dict
      4. Renders the scenario startup banner
      5. Calls five_agent_demo.run_demo(runtime_config, scenario_ctx)
      6. Renders the scenario summary banner

    No event logic lives here.
    No session management lives here.
    No governance semantics live here.
    The runtime (five_agent_demo) has no knowledge of ScenarioContext.

Lifecycle hooks:
    _before_event(n, scenario_ctx)     — apply_latency per event
    _after_event(n, event_record, scenario_ctx) — derive metrics from real outcomes
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from demo.scenarios.profiles import SCENARIOS
from demo.scenarios.engine   import build_context, scenario_to_runtime_config, render_startup_banner, render_summary_banner
from demo.scenarios.injector import apply_latency, maybe_fail

import demo.five_agent_demo as _demo


# ── Lifecycle hooks ────────────────────────────────────────────────────────────
# Called from run_demo()'s event loop when a scenario_ctx is active.
# Both are no-ops when scenario_ctx is None — baseline / direct demo runs
# are unaffected.

def _before_event(n: int, scenario_ctx) -> None:
    """
    Pre-event hook. Applies environmental latency for the active scenario.
    No-op when scenario_ctx is None.
    """
    apply_latency(scenario_ctx)


def _after_event(n: int, event_record: dict, scenario_ctx) -> None:
    """
    Post-event hook. Derives and accumulates metrics from real event outcomes.

    event_record is the dict appended by demo.record():
        { "n": int, "name": str, "result": str, "note": str }

    Metrics incremented here are derived from actual runtime results —
    not synthetic counts disconnected from governance behavior.

    No-op when scenario_ctx is None.
    """
    if scenario_ctx is None:
        return

    m      = scenario_ctx.metrics
    result = event_record.get("result", "")
    note   = event_record.get("note", "").lower()

    # Resolution path — record every event result for dominant_path()
    m.record_path(result)

    # Escalations — count real ESCALATE signals from the runtime
    if result == "ESCALATE":
        m.escalations_triggered += 1

    # Budget violations prevented — BLOCKED events with budget-related notes
    if result == "BLOCKED" and any(
        token in note for token in ("budget", "spend", "limit", "exceed")
    ):
        m.budget_violations_prevented += 1


def _on_recovery(scenario_ctx) -> None:
    """
    Called when an event raises an unexpected exception that is caught
    by run_demo()'s error handler. Increments recovery_attempts.
    No-op when scenario_ctx is None.
    """
    if scenario_ctx is None:
        return
    scenario_ctx.metrics.recovery_attempts += 1


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # ── Resolve scenario ───────────────────────────────────────────────────────
    scenario_name = "BASELINE"
    if "--scenario" in args:
        idx = args.index("--scenario")
        if idx + 1 >= len(args):
            print("  [ERROR] --scenario requires a name argument.")
            print(f"  Available: {', '.join(SCENARIOS)}")
            sys.exit(1)
        scenario_name = args[idx + 1].upper()
        # Remove --scenario NAME from args so the demo sees only its own flags
        args = args[:idx] + args[idx + 2:]

    if scenario_name not in SCENARIOS:
        print(f"  [ERROR] Unknown scenario: '{scenario_name}'")
        print(f"  Available: {', '.join(SCENARIOS)}")
        sys.exit(1)

    scenario       = SCENARIOS[scenario_name]
    scenario_ctx   = build_context(scenario)
    runtime_config = scenario_to_runtime_config(scenario)

    # ── Startup banner ─────────────────────────────────────────────────────────
    render_startup_banner(scenario_ctx)

    # ── Inject lifecycle hooks into the demo module ────────────────────────────
    # run_demo() calls _demo._before_event_hook and _demo._after_event_hook
    # if they are set. Setting them here keeps the coupling explicit and
    # reversible — the demo module defines the hook slots as None by default.
    _demo._before_event_hook = lambda n: _before_event(n, scenario_ctx)
    _demo._after_event_hook  = lambda n, rec: _after_event(n, rec, scenario_ctx)
    _demo._recovery_hook     = lambda: _on_recovery(scenario_ctx)

    # Wire tool injection for TOOL_INSTABILITY and any scenario with
    # tool_failure_rate > 0. maybe_fail() is a no-op when rate is 0.0,
    # so this is safe to set unconditionally — BASELINE incurs zero overhead.
    _demo._tool_injector = lambda tool, action: maybe_fail(scenario_ctx, tool, action)

    # ── Run the demo ───────────────────────────────────────────────────────────
    # Pass remaining args so --event N and --summary still work.
    sys.argv = [sys.argv[0]] + list(args)
    _demo.run_demo(runtime_config=runtime_config)

    # ── Summary banner ─────────────────────────────────────────────────────────
    render_summary_banner(scenario_ctx)


if __name__ == "__main__":
    main()