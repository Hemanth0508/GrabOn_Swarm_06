"""
runtime/ui/styles.py
Centralised colour map, result icons, and theme constants.

Single source of truth for all status rendering.
No other file should hard-code colour strings or icon characters.
"""

# ── Status → Rich style string ─────────────────────────────────────────────────
STATUS_STYLES: dict[str, str] = {
    "ALLOWED":  "bold green",
    "PASS":     "bold green",
    "BLOCKED":  "bold red",
    "FAIL":     "bold red",
    "ESCALATE": "bold yellow",
    "PENDING":  "bold yellow",
    "SKIP":     "dim",
    "INFO":     "cyan",
    "ERROR":    "bold red",
}

# ── Status → terminal icon ─────────────────────────────────────────────────────
# Trailing space on each icon compensates for Windows terminal Unicode
# ambiguous-width rendering (CMD, older PowerShell, Console Host).
# ✔ / ✖ are more width-stable on Windows than ✓ / ✗.
RESULT_ICONS: dict[str, str] = {
    "ALLOWED":  " \u2713 ",
    "PASS":     " \u2713 ",
    "BLOCKED":  " \u2717 ",
    "FAIL":     " \u2717 ",
    "ESCALATE": " \u25b2 ",
    "PENDING":  " \u23f3 ",
    "SKIP":     " ~  ",
    "ERROR":    " !  ",
}
# ── Panel / layout constants ───────────────────────────────────────────────────
PANEL_BORDER_STYLE  = "bright_black"   # subtle — lets content carry the weight
EVENT_TITLE_STYLE   = "bold white"
BANNER_STYLE        = "bold white"
LABEL_STYLE         = "bright_black"   # dim key names, bright values
VALUE_STYLE         = ""               # inherit terminal default
SECTION_RULE_STYLE  = "bright_black"
META_BORDER_STYLE   = "bright_black"

# Console width — governs all table sizing, wrapping, and panel width.
# 140 gives governance reasoning, UUIDs, and audit chains room to breathe.
# Propagates automatically to console.py via import.
CONSOLE_WIDTH       = 140


def status_style(result: str) -> str:
    """Return the Rich style for a given result/status string."""
    return STATUS_STYLES.get(result.upper(), "")


def result_icon(result: str) -> str:
    """Return the icon character for a given result string."""
    return RESULT_ICONS.get(result.upper(), "?")

