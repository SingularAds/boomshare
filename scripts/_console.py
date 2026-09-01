"""Terminal output that survives a Windows console.

Two problems this solves, both of which bite on a default Windows setup:

  * `sys.stdout.encoding` is often `cp1252`, so printing a character outside
    that codepage raises `UnicodeEncodeError` and kills the script. Customer
    messages and model replies routinely contain typographic quotes, dashes and
    emoji, so this is not a theoretical risk.
  * ANSI colour codes render as literal escape sequences in a console that has
    not enabled virtual terminal processing, and are noise when output is piped
    to a file.
"""

from __future__ import annotations

import os
import sys


def use_utf8() -> None:
    """Force UTF-8 output, replacing anything the console cannot render."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic terminals
                pass


def _supported() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Windows Terminal, VS Code and Git Bash all set one of these; the
        # legacy conhost does not.
        return bool(os.getenv("WT_SESSION") or os.getenv("TERM") or os.getenv("TERM_PROGRAM"))
    return True


COLOR = _supported()


def _c(code: str) -> str:
    return code if COLOR else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
BLUE = _c("\033[34m")
MAGENTA = _c("\033[35m")
CYAN = _c("\033[36m")
GREY = _c("\033[90m")


def enable_windows_ansi() -> None:
    """Ask the Windows console to interpret ANSI escapes, if it can."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 is STD_OUTPUT_HANDLE; 0x0004 is ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 - best effort only
        pass


def setup() -> None:
    """Call once, at the top of a script."""
    use_utf8()
    enable_windows_ansi()
