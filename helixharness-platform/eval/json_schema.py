"""Small dependency-free validator for the bundled eval JSON Schema.

The plugin cannot assume that the third-party ``jsonschema`` package exists in
the target project.  This module implements the Draft 2020-12 keywords used by
``reference/eval-schema-v1.json`` so the recorder and the platform validate the
same machine-readable contract.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any


class SchemaValidationError(ValueError):
    pass


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"unsupported schema reference: {reference}")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise SchemaValidationError(f"unresolved schema reference: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise SchemaValidationError(f"schema reference is not an object: {reference}")
    return value


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise SchemaValidationError(f"unsupported schema type: {expected}")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        _validate(value, _resolve_ref(root, reference), root, path)
        return

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or not all(isinstance(item, dict) for item in one_of):
            raise SchemaValidationError(f"{path}: invalid oneOf declaration")
        matches = 0
        for candidate in one_of:
            try:
                _validate(value, candidate, root, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(f"{path}: expected exactly one matching schema")

    forbidden = schema.get("not")
    if forbidden is not None:
        if not isinstance(forbidden, dict):
            raise SchemaValidationError(f"{path}: invalid not declaration")
        try:
            _validate(value, forbidden, root, path)
        except SchemaValidationError:
            pass
        else:
            raise SchemaValidationError(f"{path}: value matches a forbidden schema")

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is not in the allowed enum")

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not isinstance(expected_types, list) or not all(
            isinstance(item, str) for item in expected_types
        ):
            raise SchemaValidationError(f"{path}: invalid schema type declaration")
        if not any(_matches_type(value, item) for item in expected_types):
            raise SchemaValidationError(
                f"{path}: expected {' or '.join(expected_types)}, got {type(value).__name__}"
            )

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise SchemaValidationError(f"{path}: invalid required declaration")
        missing = [key for key in required if key not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaValidationError(f"{path}: invalid properties declaration")
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                _validate(value[key], child, root, f"{path}.{key}")
        extras = sorted(set(value) - set(properties))
        additional = schema.get("additionalProperties", True)
        if additional is False and extras:
            raise SchemaValidationError(
                f"{path}: unsupported fields: {', '.join(str(item) for item in extras)}"
            )
        if isinstance(additional, dict):
            for key in extras:
                _validate(value[key], additional, root, f"{path}.{key}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(f"{path}: more than maxItems")
        if schema.get("uniqueItems"):
            encoded = [_canonical(item) for item in value]
            if len(encoded) != len(set(encoded)):
                raise SchemaValidationError(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(f"{path}: longer than maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise SchemaValidationError(f"{path}: does not match required pattern")
        if schema.get("format") == "date-time":
            if re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                value,
            ) is None:
                raise SchemaValidationError(f"{path}: invalid date-time")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SchemaValidationError(f"{path}: invalid date-time") from exc
            if parsed.tzinfo is None:
                raise SchemaValidationError(f"{path}: date-time must include a timezone")

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        raise SchemaValidationError(f"{path}: value is below minimum {schema['minimum']}")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "maximum" in schema
        and value > schema["maximum"]
    ):
        raise SchemaValidationError(f"{path}: value is above maximum {schema['maximum']}")


def validate(instance: Any, schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema root must be an object")
    _validate(instance, schema, schema, "$")
