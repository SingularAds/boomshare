"""Opt-in pipeline tracing.

The structured logs tell you *what happened*. When you are learning the system
or debugging a flow, you also want to see *where you are in the pipeline* -
which step, in order, with the values that decided the next one.

Off by default and free when off (one boolean check). Turn it on with:

    TRACE=1

or `--trace` in the sandbox scripts. Never enable it in production: it prints
message content, which the normal request log deliberately never does.
"""

from __future__ import annotations

import os
import sys
import threading

_enabled = os.getenv("TRACE", "").lower() in {"1", "true", "yes", "on"}
_colour = sys.stderr.isatty() and not os.getenv("NO_COLOR")
_lock = threading.Lock()

# Steps are grouped by the process they belong to, so a trace reads as a story
# rather than a pile of lines.
_COLORS = {
    "webhook": "\033[36m",   # cyan   - HTTP intake
    "worker": "\033[35m",    # magenta- background processing
    "db": "\033[34m",        # blue   - persistence
    "ai": "\033[33m",        # yellow - the model
    "guard": "\033[31m",     # red    - something was refused
    "send": "\033[32m",      # green  - outbound message
    "state": "\033[36m",     # cyan   - sales state change
    "skip": "\033[90m",      # grey   - a deliberate no-op
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def enabled() -> bool:
    return _enabled


def enable(on: bool = True) -> None:
    """Turn tracing on programmatically (the sandbox scripts use this)."""
    global _enabled
    _enabled = on


def guard_production() -> None:
    """Refuse to trace in production, whatever `TRACE` was set to.

    Trace output prints raw webhook payloads and message bodies - phone numbers
    and message content that the normal logs deliberately never write. Called
    once at startup by both the API and the worker.
    """
    global _enabled
    if not _enabled:
        return
    from app.core.config import get_settings

    if get_settings().environment == "production":
        _enabled = False
        print(
            "trace: TRACE was set but tracing is disabled in production "
            "(it would log phone numbers and message content)",
            file=sys.stderr,
            flush=True,
        )


def trace(channel: str, step: str, **fields: object) -> None:
    """Print one pipeline step.

    `channel` picks the colour and reads as the subsystem; `step` is what just
    happened, in plain words; `fields` are the values that mattered.
    """
    if not _enabled:
        return

    colour = _COLORS.get(channel, "") if _colour else ""
    reset = _RESET if _colour else ""
    dim = _DIM if _colour else ""
    detail = "  ".join(f"{dim}{k}={reset}{_shorten(v)}" for k, v in fields.items() if v is not None)
    line = f"  {colour}{channel:<7}{reset} {step}"
    if detail:
        line += f"   {detail}"

    with _lock:
        print(line, file=sys.stderr, flush=True)


def _shorten(value: object, limit: int = 88) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def banner(title: str) -> None:
    """A section rule, to separate one customer message from the next."""
    if not _enabled:
        return
    dim = _DIM if _colour else ""
    bold = "\033[1m" if _colour else ""
    reset = _RESET if _colour else ""
    with _lock:
        print(f"\n{dim}{'-' * 74}{reset}", file=sys.stderr, flush=True)
        print(f"  {bold}{title}{reset}", file=sys.stderr, flush=True)


def dump(label: str, payload: object) -> None:
    """Pretty-print a whole payload. Only when tracing is on.

    For the times you want to see the entire thing rather than a summary - a
    raw webhook body, a model response. Deliberately gated: these payloads
    contain phone numbers and message content, which is exactly what the normal
    request log is careful never to write.
    """
    if not _enabled:
        return

    import json

    try:
        rendered = json.dumps(payload, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = str(payload)

    dim = _DIM if _colour else ""
    reset = _RESET if _colour else ""
    with _lock:
        print(f"  {dim}--- {label} ---{reset}", file=sys.stderr, flush=True)
        for line in rendered.splitlines():
            print(f"  {dim}|{reset} {line}", file=sys.stderr, flush=True)
