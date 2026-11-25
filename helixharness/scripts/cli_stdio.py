"""UTF-8 streams for the plugin's JSON command-line contracts."""

from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Keep a JSON CLI readable no matter which interpreter invoked it.

    `{helix_python}` appends `-X utf8` and CI exports `PYTHONUTF8=1`, so a normal
    run already is UTF-8. The contract must still hold for a caller that does not:
    these CLIs echo installation paths with `ensure_ascii=False`, a legacy Windows
    code page cannot encode CJK, and the host then receives a traceback instead of
    the JSON it parses.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict")
