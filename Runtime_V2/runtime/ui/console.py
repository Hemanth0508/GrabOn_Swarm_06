"""
runtime/ui/console.py
Singleton Rich Console instance.

All other ui modules import `con` from here.
highlight=False prevents Rich from auto-styling numbers/strings
in ways that conflict with explicit governance status colours.
"""

from rich.console import Console
from .styles import CONSOLE_WIDTH

con: Console = Console(
    highlight=False,
    width=CONSOLE_WIDTH,
)