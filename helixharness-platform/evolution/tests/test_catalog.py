from __future__ import annotations

import pytest

from evolution.catalog import CatalogError, load_catalog, save_catalog_text


def test_catalog_freezes_ai_case_and_workflow_authorization(tmp_path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        """
schema_version: evolution-capability-catalog.v1
capabilities:
  - id: workflow
    name: Workflow
    description: Workflow discipline
    selectors: [archive]
    owned_paths: [commands/]
    protected_paths: [bench/]
    case_version_ids: []
    evaluation_policy:
      case_selection: AI
      max_cases: 2
      allowed_profiles: [CHAIN_BEHAVIOR]
      auto_workflow_confirmation: true
    auto_repair: false
    growth_confirmed: false
""".strip(),
        encoding="utf-8",
    )

    capability = load_catalog(path)[0]

    assert capability.case_selection_mode == "AI"
    assert capability.case_selection_max_cases == 2
    assert capability.allowed_case_profiles == ("CHAIN_BEHAVIOR",)
    assert capability.auto_workflow_confirmation is True


def test_catalog_rejects_unbounded_ai_case_limit(tmp_path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        """
schema_version: evolution-capability-catalog.v1
capabilities:
  - id: workflow
    name: Workflow
    description: Workflow discipline
    selectors: [archive]
    owned_paths: [commands/]
    protected_paths: [bench/]
    case_version_ids: []
    evaluation_policy:
      case_selection: AI
      max_cases: 100
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="max_cases"):
        load_catalog(path)


def test_catalog_rejects_string_workflow_authorization(tmp_path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        """
schema_version: evolution-capability-catalog.v1
capabilities:
  - id: workflow
    name: Workflow
    description: Workflow discipline
    selectors: [archive]
    owned_paths: [commands/]
    protected_paths: [bench/]
    case_version_ids: []
    evaluation_policy:
      case_selection: AI
      auto_workflow_confirmation: "false"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="must be a boolean"):
        load_catalog(path)


def test_catalog_save_validates_before_replacing_the_source(tmp_path) -> None:
    path = tmp_path / "capabilities.yaml"
    valid = """\
schema_version: evolution-capability-catalog.v1
capabilities:
  - id: workflow
    name: Workflow
    description: Workflow rules
    selectors: [workflow]
    owned_paths: [commands/]
    protected_paths: [eval/]
    case_version_ids: []
    evaluation_policy:
      case_selection: AI
      max_cases: 1
      allowed_profiles: []
      auto_workflow_confirmation: true
    auto_repair: false
    growth_confirmed: false
"""

    saved = save_catalog_text(path, valid)

    assert saved[0].capability_id == "workflow"
    before = path.read_bytes()
    with pytest.raises(CatalogError, match="unsupported"):
        save_catalog_text(path, "schema_version: broken\ncapabilities: []\n")
    assert path.read_bytes() == before
