"""Shared test-only configuration."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def framework_eval_test_case_source_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep synthetic Cases in tests instead of the production runtime package."""

    monkeypatch.setenv(
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CASE_SOURCE_ROOT",
        str(Path(__file__).parent / "fixtures"),
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_LEGACY_WRITE_ENABLED", "true")
