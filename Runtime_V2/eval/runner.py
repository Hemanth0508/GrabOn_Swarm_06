"""
eval/runner.py
Agent Governance v2 — Eval Runner

Runs all assertions after every demo execution.
Called from demo/five_agent_demo.py at the end of the run.
"""

from eval.assertions import run_evals
from runtime.ui.tables import eval_table
from runtime.ui.panels import banner


def run_all_evals(ctx: dict, event_results: list) -> dict:
    """
    Execute all assertions, render a Rich eval table, and return
    structured eval stats for downstream consumers (e.g. runtime_summary_panel).

    Args:
        ctx:           Demo context dict. Must contain key 'orch_sid'.
        event_results: List of event dicts with keys: n, name, result.

    Returns:
        dict with keys:
            assertions_passed  int
            assertions_total   int
            assertions_skipped int
            results            list[dict]   full assertion results
    """
    # Derive the set of event numbers that actually ran this session.
    # Passed to run_evals() so assertions dependent on unexecuted events
    # are marked SKIPPED rather than FAIL.
    executed_events = {r["n"] for r in event_results}

    results = run_evals(
        ctx["orch_sid"],
        event_results,
        db_path=None,
        executed_events=executed_events,
    )

    skipped = sum(1 for r in results if r.get("skipped"))
    passed  = sum(1 for r in results if r["passed"] and not r.get("skipped"))
    total   = len(results)

    banner("EVALUATION RESULTS")
    eval_table(results)

    return {
        "assertions_passed":  passed,
        "assertions_total":   total,
        "assertions_skipped": skipped,
        "results":            results,
    }