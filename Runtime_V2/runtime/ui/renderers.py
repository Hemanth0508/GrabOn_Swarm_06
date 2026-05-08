"""
runtime/ui/renderers.py
Reusable low-level render helpers.

These are the building blocks used by panels.py, tables.py, and
the wired replacements for show() / result_line() in five_agent_demo.py.

Rules:
  - All output goes through `con` from console.py.
  - No Rich markup is constructed outside this file or tables/panels.
  - Event function logic is never imported here.
"""

from rich.text import Text
from rich.rule import Rule
from rich.padding import Padding
import json

from .console import con
from .styles import status_style, result_icon, LABEL_STYLE, SECTION_RULE_STYLE


# ── Label / value line ─────────────────────────────────────────────────────────

def _compact(value, max_len: int = 120) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            s = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            s = str(value)
    else:
        s = str(value)
    s = " ".join(s.split())
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def show(label: str, value, indent: int = 2) -> None:
    """
    Styled replacement for the legacy show() function.

    Renders as:   <label>: <value>
    with the label dimmed and value in default colour.
    Long values wrap automatically via Rich's console width.
    """
    pad = " " * indent
    t = Text()
    t.append(f"{pad}{label:<14} ", style=LABEL_STYLE)
    t.append(_compact(value))
    con.print(t, overflow="crop", no_wrap=True)


# ── Result line ────────────────────────────────────────────────────────────────

def result_line(result: str, reason: str, indent: int = 2) -> None:
    """
    Styled replacement for the legacy result_line() function.

    Renders as:   [✓] ALLOWED: <reason>
    with the status badge coloured according to STATUS_STYLES.
    """
    pad    = " " * indent
    icon   = result_icon(result)
    style  = status_style(result)

    t = Text()
    t.append(f"{pad}[ {icon.strip()} ]  ", style=style)
    t.append(f"{result}: ", style=style)
    t.append(_compact(reason, max_len=180))
    con.print(t, overflow="crop", no_wrap=True)


# ── Separator (dots row) ───────────────────────────────────────────────────────

def separator() -> None:
    """Renders the ··· separator used between Path A / Path B blocks."""
    con.print(f"  {'·' * 64}", style=SECTION_RULE_STYLE)


# ── Section rule ───────────────────────────────────────────────────────────────

def section_rule(title: str = "") -> None:
    """Thin horizontal rule, optionally titled."""
    con.print(Rule(title, style=SECTION_RULE_STYLE))


# ── State delta block ──────────────────────────────────────────────────────────

def state_delta(pairs: list[tuple[str, str]]) -> None:
    """
    Renders a compact STATE DELTA block.

    pairs: list of (key, "before → after") tuples.

    Example output:
        STATE DELTA
        ────────────────────────────────────
        budget_spent    0.0 → 250.0
        remaining       500.0 → 250.0
    """
    con.print()
    con.print("    [bold]STATE DELTA[/bold]")
    con.print(f"    {'─' * 40}", style=SECTION_RULE_STYLE)
    for key, delta in pairs:
        t = Text()
        t.append(f"    {key:<20}", style=LABEL_STYLE)
        t.append(delta)
        con.print(t)
    con.print()




# ── UUID / hash shortener ──────────────────────────────────────────────────────

def fmt_id(value: str, length: int = 8) -> str:
    """
    Shorten a UUID or hash to a readable prefix for display.
    Strips hyphens-then-truncates for UUIDs, plain truncate for hashes.

    Usage in demo:
        show("action_id", fmt_id(d.action_id))
    """
    clean = str(value).replace("-", "")
    return clean[:length] + "..."

# ── Plain print passthrough ────────────────────────────────────────────────────

def println(text: str = "") -> None:
    """Print a plain line through the singleton console."""
    con.print(text)
