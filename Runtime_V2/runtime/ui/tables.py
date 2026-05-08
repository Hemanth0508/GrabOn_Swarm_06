"""
runtime/ui/tables.py
Rich Table renderers.

Public functions:
  eval_table(results)            — replaces flat PASS | ... lines in runner.py
  summary_table(event_results)   — replaces print_summary() plain table
  benchmark_table(scenarios)     — replaces the manual event_10 print loop
  meta_block(pairs, title)       — compact key/value table for metadata display
"""

from rich.table import Table
from rich.text import Text
from rich.panel import Panel

from .console import con
from .styles import (
    STATUS_STYLES, PANEL_BORDER_STYLE,
    status_style, result_icon,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _status_cell(status: str) -> Text:
    """Coloured status badge for a table cell."""
    t = Text(status, style=status_style(status))
    return t


def _icon_cell(result: str) -> Text:
    """Icon + result string for summary table Result column."""
    icon  = result_icon(result)
    style = status_style(result)
    t = Text()
    t.append(f"{icon} {result}", style=style)
    return t


# ── Eval table ─────────────────────────────────────────────────────────────────

def eval_table(results: list[dict]) -> None:
    """
    Renders the evaluation results as a Rich table.

    Each result dict must have keys: passed (bool), eval (str), detail (str).
    Optional key: skipped (bool) — renders as SKIP status instead of PASS/FAIL.
    """
    table = Table(
        show_header=True,
        header_style="bold",
        border_style=PANEL_BORDER_STYLE,
        show_lines=False,
        box=_BOX_HEAVY_HEAD,
        safe_box=True,
        padding=(0, 1),
        expand=True,
    )

    table.add_column("Status",    width=8,  no_wrap=True)
    table.add_column("Assertion", min_width=38)
    table.add_column("Detail",    min_width=60, overflow="fold")

    for r in results:
        if r.get("skipped"):
            status = "SKIP"
        else:
            status = "PASS" if r["passed"] else "FAIL"
        table.add_row(
            _status_cell(status),
            r["eval"],
            r["detail"],
        )

    passed  = sum(1 for r in results if r["passed"] and not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    failed  = len(results) - passed - skipped

    con.print()
    con.print(Panel(
        table,
        title="[bold]EVALUATION RESULTS[/bold]",
        border_style=PANEL_BORDER_STYLE,
        padding=(0, 0),
        safe_box=True,
    ))

    # Footer counts
    t = Text()
    t.append(f"  {passed} PASSED", style=status_style("PASS"))
    t.append("  ")
    if failed:
        t.append(f"{failed} FAILED", style=status_style("FAIL"))
    else:
        t.append("0 FAILED", style="dim")
    if skipped:
        t.append("  ")
        t.append(f"{skipped} SKIPPED", style=status_style("SKIP"))
    t.append(f"  (total: {len(results)})", style="dim")
    con.print(t)
    con.print()


# ── Event summary table ────────────────────────────────────────────────────────

def summary_table(event_results: list[dict]) -> None:
    """
    Renders the end-of-demo event summary as a Rich table.

    Replaces the manual f-string loop in print_summary().

    Each event_results dict must have keys: n, name, result, note.
    """
    table = Table(
        show_header=True,
        header_style="bold",
        border_style=PANEL_BORDER_STYLE,
        show_lines=False,
        box=_BOX_HEAVY_HEAD,
        safe_box=True,
        padding=(0, 1),
        expand=True,
    )

    table.add_column("No",     width=3,  no_wrap=True, justify="right")
    table.add_column("Event",  min_width=34)
    table.add_column("Result", width=11, no_wrap=True)
    table.add_column("Note",   min_width=32, overflow="fold")

    for r in event_results:
        table.add_row(
            str(r["n"]),
            r["name"],
            _icon_cell(r["result"]),
            r["note"],
        )

    blocked  = sum(1 for r in event_results if r["result"] == "BLOCKED")
    allowed  = sum(1 for r in event_results if r["result"] == "ALLOWED")
    pending  = sum(1 for r in event_results if r["result"] == "PENDING")
    escalate = sum(1 for r in event_results if r["result"] == "ESCALATE")
    skipped  = sum(1 for r in event_results if r["result"] == "SKIP")

    con.print(table)
    con.print()

    # Count summary line
    t = Text("  ")
    t.append(f"BLOCKED: {blocked}",   style=status_style("BLOCKED"))
    t.append("  ")
    t.append(f"ALLOWED: {allowed}",   style=status_style("ALLOWED"))
    t.append("  ")
    t.append(f"PENDING: {pending}",   style=status_style("PENDING"))
    t.append("  ")
    t.append(f"ESCALATE: {escalate}", style=status_style("ESCALATE"))
    t.append("  ")
    t.append(f"SKIPPED: {skipped}",   style="dim")
    con.print(t)
    con.print()

    # Architecture footnote
    con.print("  Architecture: stateful governance runtime",          style="dim")
    con.print("  State store:  SQLite WAL (dev) → Spanner/CockroachDB (prod)", style="dim")
    con.print("  All decisions: deterministic, independent of model reasoning", style="dim")
    con.print()


def timeline_table(event_results: list[dict]) -> None:
    """
    Compact runtime progression timeline.
    """
    t = Table(
        show_header=False,
        border_style=PANEL_BORDER_STYLE,
        box=_BOX_SIMPLE,
        padding=(0, 1),
        expand=False,
    )
    t.add_column("Time", width=8, no_wrap=True, style="bright_black")
    t.add_column("Status", width=9, no_wrap=True)
    t.add_column("Event", min_width=24, overflow="crop")

    for r in event_results:
        ts = str(r.get("ts", ""))[:8] if r.get("ts") else "--:--:--"
        t.add_row(ts, _status_cell(r["result"]), r["name"])

    con.print("[bold]TIMELINE[/bold]")
    con.print(t)
    con.print()


# ── Benchmark table ────────────────────────────────────────────────────────────

def benchmark_table(scenarios: list[tuple]) -> None:
    """
    Renders the three-way benchmark comparison (Event 10).

    Each scenario tuple: (name, stateful, stateless, raw_llm)
    """
    table = Table(
        show_header=True,
        header_style="bold",
        border_style=PANEL_BORDER_STYLE,
        show_lines=False,
        box=_BOX_SIMPLE,
        safe_box=True,
        padding=(0, 1),
    )

    table.add_column("Scenario",  min_width=36)
    table.add_column("Stateful",  width=10, justify="center")
    table.add_column("Stateless", width=10, justify="center")
    table.add_column("Raw LLM",   width=10, justify="center")

    for name, stateful, stateless, raw in scenarios:
        table.add_row(
            name,
            Text(stateful,  style=status_style(stateful.rstrip("*"))),
            Text(stateless, style=status_style(stateless.rstrip("*"))),
            Text(raw,       style="dim"),
        )

    con.print(table)


# ── Metadata block ─────────────────────────────────────────────────────────────

def meta_block(pairs: list[tuple[str, str]], title: str = "") -> None:
    """
    Compact two-column key/value table.

    Used for structured metadata display where individual show() calls
    would be noisy (e.g. session health in Event 20, step details in Event 21).

    pairs: list of (key, value) string tuples.
    title: optional label printed above the block.
    """
    table = Table(
        show_header=False,
        border_style=PANEL_BORDER_STYLE,
        show_lines=False,
        box=_BOX_SIMPLE,
        safe_box=True,
        padding=(0, 1),
    )
    table.add_column("Key",   min_width=24, style="bright_black")
    table.add_column("Value", min_width=20)

    for k, v in pairs:
        table.add_row(k, str(v))

    if title:
        con.print(f"    [bright_black]{title}[/bright_black]")
    con.print(table)


# ── Box styles (inline to avoid extra dependency) ──────────────────────────────
# Rich's built-in box constants, accessed directly to avoid import clutter.

from rich import box as _box

_BOX_HEAVY_HEAD = _box.HEAVY_HEAD
_BOX_SIMPLE     = _box.SIMPLE
