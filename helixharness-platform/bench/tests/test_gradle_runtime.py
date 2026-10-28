from __future__ import annotations

from pathlib import Path

import pytest

from bench.gradle_runtime import (
    GRADLE_HEAP_MB_ENV,
    GradleRuntimeConfigurationError,
    configure_evaluator_gradle_home,
    evaluator_gradle_heap_mb,
)


def test_default_gradle_heap_is_four_gibibytes() -> None:
    assert evaluator_gradle_heap_mb({}) == 4096


def test_gradle_heap_accepts_bounded_platform_override(tmp_path: Path) -> None:
    configure_evaluator_gradle_home(tmp_path, environment={GRADLE_HEAP_MB_ENV: "5120"})

    assert (tmp_path / "gradle.properties").read_text(encoding="utf-8") == (
        "org.gradle.jvmargs=-Xmx5120m\norg.gradle.daemon=false\n"
    )


@pytest.mark.parametrize("value", ("2047", "6145", "invalid"))
def test_gradle_heap_rejects_unsafe_platform_override(value: str) -> None:
    with pytest.raises(GradleRuntimeConfigurationError):
        evaluator_gradle_heap_mb({GRADLE_HEAP_MB_ENV: value})
