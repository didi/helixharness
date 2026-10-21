from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench.scenario_worker import cached_repositories, parse_repositories, worker_workload_kind


def test_repository_mapping_requires_unique_git_checkouts(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", str(repository)), check=True, capture_output=True)

    assert parse_repositories([f"business={repository}"]) == {"business": repository.resolve()}
    assert cached_repositories(tmp_path) == {"repository": repository.resolve()}
    with pytest.raises(ValueError, match="Duplicate"):
        parse_repositories([f"business={repository}", f"business={repository}"])
    with pytest.raises(ValueError, match="Git checkout"):
        parse_repositories([f"missing={tmp_path / 'missing'}"])


def test_managed_worker_name_resolves_its_isolated_workload() -> None:
    assert worker_workload_kind("scenario-EVOLUTION_EVALUATION-1", None) == "EVOLUTION_EVALUATION"
    assert worker_workload_kind("local-worker", None) == "FORMAL_EVALUATION"
    assert worker_workload_kind("local-worker", "EVOLUTION_EVALUATION") == "EVOLUTION_EVALUATION"
