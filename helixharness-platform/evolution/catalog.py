"""Git-versioned, human-owned capability catalog."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from evolution.models import Capability


class CatalogError(ValueError):
    """Raised when the catalog cannot be treated as an explicit contract."""


def load_catalog(path: str | Path) -> tuple[Capability, ...]:
    catalog_path = Path(path)
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != "evolution-capability-catalog.v1":
        raise CatalogError("unsupported capability catalog schema")
    items = raw.get("capabilities")
    if not isinstance(items, list):
        raise CatalogError("capabilities must be a list")
    seen: set[str] = set()
    result: list[Capability] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CatalogError(f"capabilities[{index}] must be an object")
        capability_id = _required_text(item, "id", index)
        if capability_id in seen:
            raise CatalogError(f"duplicate capability id: {capability_id}")
        seen.add(capability_id)
        evaluation_policy = item.get("evaluation_policy", {})
        if not isinstance(evaluation_policy, dict):
            raise CatalogError(f"capabilities[{index}].evaluation_policy must be an object")
        case_selection_mode = str(evaluation_policy.get("case_selection", "EXPLICIT")).strip().upper()
        if case_selection_mode not in {"EXPLICIT", "AI"}:
            raise CatalogError(
                f"capabilities[{index}].evaluation_policy.case_selection must be EXPLICIT or AI"
            )
        max_cases = evaluation_policy.get("max_cases", 3)
        if not isinstance(max_cases, int) or isinstance(max_cases, bool) or not 1 <= max_cases <= 10:
            raise CatalogError(
                f"capabilities[{index}].evaluation_policy.max_cases must be an integer from 1 to 10"
            )
        auto_workflow_confirmation = evaluation_policy.get(
            "auto_workflow_confirmation",
            False,
        )
        if not isinstance(auto_workflow_confirmation, bool):
            raise CatalogError(
                "capabilities["
                f"{index}].evaluation_policy.auto_workflow_confirmation must be a boolean"
            )
        result.append(
            Capability(
                capability_id=capability_id,
                name=_required_text(item, "name", index),
                description=_required_text(item, "description", index),
                selectors=_strings(item.get("selectors"), "selectors", index),
                owned_paths=_strings(item.get("owned_paths"), "owned_paths", index),
                protected_paths=_strings(item.get("protected_paths"), "protected_paths", index),
                case_version_ids=_strings(item.get("case_version_ids"), "case_version_ids", index),
                case_selection_mode=case_selection_mode,
                case_selection_max_cases=max_cases,
                allowed_case_profiles=_strings(
                    evaluation_policy.get("allowed_profiles", []),
                    "evaluation_policy.allowed_profiles",
                    index,
                ),
                auto_workflow_confirmation=auto_workflow_confirmation,
                auto_repair=bool(item.get("auto_repair", False)),
                growth_confirmed=bool(item.get("growth_confirmed", False)),
            )
        )
    return tuple(result)


def save_catalog_text(path: str | Path, content: str) -> tuple[Capability, ...]:
    """Validate and atomically save the human-owned catalog text."""

    catalog_path = Path(path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{catalog_path.name}.", dir=catalog_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        capabilities = load_catalog(temporary_name)
        os.replace(temporary_name, catalog_path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return capabilities


def _required_text(item: dict[str, object], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"capabilities[{index}].{key} must be non-empty")
    return value.strip()


def _strings(value: object, key: str, index: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise CatalogError(f"capabilities[{index}].{key} must be a list of strings")
    return tuple(item.strip() for item in value)
