"""Terminal output that can't crash on encoding."""

from __future__ import annotations

import sys


def harden_streams() -> None:
    """Replace characters stdout/stderr can't encode instead of raising.

    Windows reports the NUL device as a terminal and encodes it (and legacy
    consoles) as cp1252, so a spinner frame or a "✗" would otherwise crash the
    command with UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # detached or closed stream
                pass
