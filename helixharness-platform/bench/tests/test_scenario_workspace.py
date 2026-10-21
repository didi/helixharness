from __future__ import annotations

import pytest

from bench.scenario_workspace import (
    EMPTY_GIT_DIFF_SHA256,
    MULTI_REPOSITORY,
    SINGLE_REPOSITORY,
    scenario_workspace,
)


def _repository(repository_id: str, *, mount_path: str, impact: str = "WRITE") -> dict[str, str]:
    return {
        "repository_id": repository_id,
        "mount_path": mount_path,
        "impact": impact,
        "base_commit": "1" * 40,
        "solution_commit": "1" * 40 if impact == "READ" else "2" * 40,
        "solution_patch_sha256": EMPTY_GIT_DIFF_SHA256 if impact == "READ" else "3" * 64,
    }


def test_legacy_repository_normalizes_without_changing_its_manifest() -> None:
    repository = _repository("legacy-repository", mount_path="ignored")
    manifest = {"repository": repository.copy()}

    workspace = scenario_workspace(manifest)

    assert workspace.repository_mode == SINGLE_REPOSITORY
    assert workspace.primary_repository_id == "legacy-repository"
    assert workspace.repositories[0].mount_path == "."
    assert manifest == {"repository": repository}


def test_explicit_repository_modes_are_mutually_exclusive() -> None:
    repository = _repository("primary", mount_path="primary")
    multi_workspace = {
        "primary_repository_id": "primary",
        "repositories": [repository, _repository("context", mount_path="context", impact="READ")],
    }

    with pytest.raises(ValueError, match="must not declare workspace"):
        scenario_workspace(
            {
                "repository_mode": SINGLE_REPOSITORY,
                "repository": repository,
                "workspace": multi_workspace,
            }
        )

    with pytest.raises(ValueError, match="must not declare single repository"):
        scenario_workspace(
            {
                "repository_mode": MULTI_REPOSITORY,
                "repository": repository,
                "workspace": multi_workspace,
            }
        )


def test_multi_repository_accepts_write_and_unchanged_read_members() -> None:
    workspace = scenario_workspace(
        {
            "repository_mode": MULTI_REPOSITORY,
            "workspace": {
                "primary_repository_id": "primary",
                "repositories": [
                    _repository("primary", mount_path="primary"),
                    _repository("context", mount_path="context", impact="READ"),
                ],
            },
        }
    )

    assert workspace.repository_mode == MULTI_REPOSITORY
    assert [item.repository_id for item in workspace.repositories] == ["primary", "context"]
    assert workspace.repositories[1].solution_patch_sha256 == EMPTY_GIT_DIFF_SHA256
