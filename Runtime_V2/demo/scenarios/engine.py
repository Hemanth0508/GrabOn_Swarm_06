"""
demo/scenarios/engine.py
Scenario Engine core.

Responsibilities:
  1. ScenarioContext dataclass — the single context object passed through
     the scenario lifecycle. The runtime never sees this object.

  2. build_context() — factory that wires isolated RNG and fresh metrics
     to a Scenario, producing a fully initialised ScenarioContext.

  3. scenario_to_runtime_config() — the ONLY coupling point between the
     scenario engine and the runtime. Produces a plain dict that the runtime
     consumes via setup_demo(runtime_config=...). The runtime has no knowledge
     of Scenario, ScenarioContext, or this module.

  4. render_startup_banner() / render_summary_banner() — Rich UI output
     using the existing runtime UI layer (con, banner). No new UI primitives.

The runtime (five_agent_demo.py) does NOT import this module.
"""

from __future__ import annotations
import random
from dataclasses import dataclass

from .profiles import Scenario, RC_BUDGET_LIMIT, RC_APPROVAL_THRESHOLD, RC_ESCALATION_THRESHOLD, RC_PROTOCOL, RC_RETRY_BUDGET
from .metrics  import ScenarioMetrics

# UI layer — same layer used by the runtime itself
from runtime.ui.console import con
from runtime.ui.panels  import banner


# ── ScenarioContext ────────────────────────────────────────────────────────────

@dataclass
class ScenarioContext:
    """
    Scenario execution context.

    Travels alongside a run_demo() invocation. The runtime never receives
    this object — it is consumed only by the lifecycle hooks in scenario_runner.py
    and the rendering functions in this module.

    Fields:
        scenario   The active Scenario profile (immutable).
        metrics    Mutable run metrics accumulated by lifecycle hooks.
        rng        Isolated Random instance seeded with scenario.name.
                   Never touches global random state.
    """
    scenario: Scenario
    metrics:  ScenarioMetrics
    rng:      random.Random


# ── Factory ────────────────────────────────────────────────────────────────────

def build_context(scenario: Scenario) -> ScenarioContext:
    """
    Construct a fully initialised ScenarioContext for a scenario run.

    RNG is seeded with scenario.name (a string constant from profiles.py).
    Same scenario name → same seed → identical injection sequence every run.
    random.seed() is never called — global RNG state is untouched.
    """
    return ScenarioContext(
        scenario = scenario,
        metrics  = ScenarioMetrics(),
        rng      = random.Random(scenario.name),
    )


# ── Config translation layer ───────────────────────────────────────────────────

def scenario_to_runtime_config(scenario: Scenario) -> dict:
    """
    Translate a Scenario into a generic runtime_config dict.

    This is the sole coupling point between the scenario engine and the runtime.
    The runtime reads this dict via setup_demo(runtime_config=...) with safe
    defaults for every key — it does not know or care that a Scenario produced it.

    A CI pipeline, benchmark harness, or human operator could produce an
    identical dict without importing the scenario engine.
    """
    return {
        RC_BUDGET_LIMIT:         scenario.budget_limit,
        RC_APPROVAL_THRESHOLD:   scenario.approval_threshold,
        RC_ESCALATION_THRESHOLD: scenario.escalation_threshold,
        RC_PROTOCOL:             scenario.protocol,
        RC_RETRY_BUDGET:         scenario.retry_budget,
    }


# ── Rich UI rendering ──────────────────────────────────────────────────────────

def render_startup_banner(ctx: ScenarioContext) -> None:
    """
    Print the scenario activation header using the existing runtime UI layer.

    Output example:
        ── SCENARIO ACTIVE ──────────────────────────────────────────
          Name:          TOOL_INSTABILITY
          Budget:        500.0
          Failure rate:  0.4
          Latency:       HIGH
          Retry budget:  2
          Protocol:      STRICT

    Governance event logs are not touched. This prints before run_demo() starts.
    """
    s = ctx.scenario
    banner("SCENARIO ACTIVE")
    con.print()
    _kv("Name",          s.name)
    _kv("Budget",        f"${s.budget_limit:.2f}")
    _kv("Failure rate",  f"{s.tool_failure_rate:.0%}")
    _kv("Latency",       s.latency)
    _kv("Retry budget",  str(s.retry_budget))
    _kv("Protocol",      s.protocol)
    con.print()


def render_summary_banner(ctx: ScenarioContext) -> None:
    """
    Print the scenario run summary using the existing runtime UI layer.

    Metrics are derived from real runtime outcomes accumulated in lifecycle
    hooks — not synthetic counts. Called after run_demo() completes.

    Output example:
        ── SCENARIO RUN SUMMARY ─────────────────────────────────────
          Recovery attempts:           2
          Escalations triggered:       1
          Budget violations prevented: 0
          Tool timeouts injected:      3
          Dominant resolution path:    ALLOWED
    """
    banner("SCENARIO RUN SUMMARY")
    con.print()
    for label, value in ctx.metrics.as_display_pairs():
        _kv(label, value)
    con.print()


# ── Internal helper ────────────────────────────────────────────────────────────

def _kv(label: str, value: str) -> None:
    """Print a single key/value line through the singleton Rich console."""
    from rich.text import Text
    t = Text()
    t.append(f"  {label + ':':<30}", style="bright_black")
    t.append(value)
    con.print(t)