"""Strict JSON decoding shared by HelixHarness telemetry components."""

from __future__ import annotations

import json
from typing import Any, Iterable, TextIO


class DuplicateJSONKeyError(json.JSONDecodeError):
    """Raised when any JSON object contains the same key more than once."""

    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON object key: {key}", "", 0)


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(key)
        result[key] = value
    return result


def loads(value: str | bytes | bytearray) -> Any:
    """Decode JSON while rejecting duplicate keys at every nesting depth."""
    return json.loads(value, object_pairs_hook=_unique_object)


def load(handle: TextIO) -> Any:
    """Read and strictly decode JSON from a text stream."""
    return loads(handle.read())
