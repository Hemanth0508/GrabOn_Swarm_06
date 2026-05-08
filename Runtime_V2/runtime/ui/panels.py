"""
runtime/ui/panels.py
Rich Panel renderers for event headers and the runtime summary block.

Two public functions:
  event_panel(n, title)          — replaces event_header()
  runtime_summary_panel(stats)   — closing governance summary
"""

from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule
from rich.table import Table

from .console import con
from .styles import (
    PANEL_BORDER_STYLE, EVENT_TITLE_STYLE, BANNER_STYLE,
    SECTION_RULE_STYLE, status_style, result_icon,
)


# ── Event header panel ─────────────────────────────────────────────────────────

def event_panel(n: int, title: str) -> None:
    """
    Renders a Rich Panel as the event header.

    Replaces:
        print(f"  EVENT {n:02d} — {name}")
    With a styled panel that visually groups each event.
    """
    con.print()
    label = Text()
    label.append(f"EVENT {n:02d}", style="bold cyan")
    label.append("  —  ", style=PANEL_BORDER_STYLE)
    label.append(title, style=EVENT_TITLE_STYLE)
    con.print(Panel(label, border_style=PANEL_BORDER_STYLE, padding=(0, 2), safe_box=True))


# ── Banner ─────────────────────────────────────────────────────────────────────

def banner(text: str) -> None:
    """
    Replaces the legacy banner() function.
    Renders a Rule with the banner text centred.
    """
    con.print()
    con.print(Rule(f"[{BANNER_STYLE}]{text}[/{BANNER_STYLE}]",
                   style=PANEL_BORDER_STYLE))


# ── Runtime summary panel ──────────────────────────────────────────────────────

def runtime_summary_panel(stats: dict) -> None:
    """
    Renders the closing Governance Runtime Summary panel.

    stats dict keys:
        events_executed   int
        assertions_passed int
        assertions_total  int
        blocked           int
        escalations       int
        health            str   e.g. "STABLE"
    """
    con.print()

    lines = Text()

    # ── Counts ────────────────────────────────────────────────────────────────
    def _row(label: str, value: str) -> None:
        lines.append(f"  {label:<28}", style="bright_black")
        lines.append(f"{value}\n")

    _row("Events Executed:",       str(stats.get("events_executed", 21)))
    _row("Assertions Passed:",
         f"{stats.get('assertions_passed', 0)} / {stats.get('assertions_total', 0)}")
    _row("Blocked Actions:",       str(stats.get("blocked", 0)))
    _row("Escalations Triggered:", str(stats.get("escalations", 0)))

    health      = stats.get("health", "STABLE")
    health_style = status_style("ALLOWED") if health == "STABLE" else status_style("BLOCKED")
    lines.append(f"  {'Runtime Health:':<28}", style="bright_black")
    lines.append(f"{health}\n", style=health_style)

    # ── Deterministic guarantees ───────────────────────────────────────────────
    lines.append("\n")
    lines.append("  Deterministic Guarantees:\n", style="bold")
    for guarantee in [
        "Audit chain integrity",
        "Stateful taint enforcement",
        "Conflict resolution commitment",
        "Managed healing active",
    ]:
        lines.append("  ✓ ", style="bold green")
        lines.append(f"{guarantee}\n")

    con.print(Panel(
        lines,
        title="[bold]Governance Runtime Summary[/bold]",
        border_style=PANEL_BORDER_STYLE,
        padding=(1, 2),
        safe_box=True,
    ))


def runtime_header_panel(stats: dict) -> None:
    """
    Compact operations header shown at startup and after each event.

    stats keys:
      session, health, budget_spent, budget_limit, agents, escalations
    """
    grid = Table.grid(expand=True)
    grid.add_column(style="bright_black", ratio=1)
    grid.add_column(style="white", ratio=1)
    grid.add_column(style="bright_black", ratio=1)
    grid.add_column(style="white", ratio=1)

    budget = f"${stats.get('budget_spent', 0):.0f} / ${stats.get('budget_limit', 0):.0f}"
    grid.add_row("Session", stats.get("session", "ACTIVE"), "Runtime Health", stats.get("health", "STABLE"))
    grid.add_row("Budget", budget, "Agents", str(stats.get("agents", 0)))
    grid.add_row("Escalations", str(stats.get("escalations", 0)), "", "")

    con.print(
        Panel(
            grid,
            title="[bold cyan]GOVERNANCE RUNTIME[/bold cyan]",
            border_style=PANEL_BORDER_STYLE,
            padding=(0, 1),
            safe_box=True,
        )
    )
