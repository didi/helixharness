"""Web adapter test configuration."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def scenario_test_source_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Web tests isolated from the production Bench Case package."""

    project_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv(
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CASE_SOURCE_ROOT",
        str(project_root / "bench" / "tests" / "fixtures"),
    )
