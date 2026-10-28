"""Validate the executing plugin against the source bound by the current host session."""
from __future__ import annotations

import json
import os
from pathlib import Path

BINDING_ENV = "HELIXHARNESS_BOUND_PLUGIN_ROOT"


class SourceBindingError(ValueError):
    pass


def validate_source(root: Path, expected: str | Path | None = None) -> Path:
    actual = root.resolve()
    # Both constraints apply: a command-line argument cannot replace session identity.
    for candidate in (os.environ.get(BINDING_ENV), expected):
        if candidate is None:
            continue
        if not str(candidate).strip():
            raise SourceBindingError("bound plugin root is empty")
        bound = Path(candidate).expanduser()
        if not bound.is_absolute() or bound.resolve() != actual:
            raise SourceBindingError("executing plugin differs from the session-bound plugin root")
    return actual


def guard_source(root: Path, expected: str | Path | None = None) -> Path:
    """CLI boundary: reject a different checkout before project reads or writes."""
    try:
        return validate_source(root, expected)
    except SourceBindingError as exc:
        print(json.dumps({"status": "blocked", "code": "helixharness_host_contract_invalid",
                          "reason": "plugin_source_mismatch", "message": str(exc)}))
        raise SystemExit(2) from None
