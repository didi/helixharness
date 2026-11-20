"""OpenCode host detection for optional diagnostics.

OpenCode currently declares ``telemetry`` and ``plugin_update`` as
``unsupported``, so this module intentionally exposes only a detector. Business
commands bind their Host Context through the installed plugin; the detector is
used only by development diagnostics and the Host Context CLI.
"""

from __future__ import annotations

from typing import Any


def detect_host(environment: Any) -> int | None:
    """Return a confidence score only for explicit OpenCode runtime markers."""

    if environment.get("OPENCODE_SESSION_ID"):
        return 100
    return 80 if environment.get("OPENCODE") else None
