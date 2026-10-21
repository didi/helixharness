"""Evaluator-owned Gradle resource policy shared by every execution entrypoint."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

GRADLE_HEAP_MB_ENV = "HELIXHARNESS_PLATFORM_EVALUATION_GRADLE_MAX_HEAP_MB"
DEFAULT_GRADLE_HEAP_MB = 4096
MIN_GRADLE_HEAP_MB = 2048
MAX_GRADLE_HEAP_MB = 6144


class GradleRuntimeConfigurationError(ValueError):
    """Raised when the configured evaluator Gradle heap is unsafe."""


def evaluator_gradle_heap_mb(environment: Mapping[str, str] | None = None) -> int:
    """Return the bounded evaluator Gradle heap configured by the platform."""

    values = os.environ if environment is None else environment
    raw_value = values.get(GRADLE_HEAP_MB_ENV, str(DEFAULT_GRADLE_HEAP_MB)).strip()
    try:
        heap_mb = int(raw_value)
    except ValueError as exc:
        raise GradleRuntimeConfigurationError(f"{GRADLE_HEAP_MB_ENV} must be an integer") from exc
    if not MIN_GRADLE_HEAP_MB <= heap_mb <= MAX_GRADLE_HEAP_MB:
        raise GradleRuntimeConfigurationError(
            f"{GRADLE_HEAP_MB_ENV} must be between {MIN_GRADLE_HEAP_MB} and {MAX_GRADLE_HEAP_MB}"
        )
    return heap_mb


def configure_evaluator_gradle_home(
    target: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Apply evaluator-owned resource limits to an isolated Gradle home."""

    heap_mb = evaluator_gradle_heap_mb(environment)
    target.mkdir(parents=True, exist_ok=True)
    (target / "gradle.properties").write_text(
        f"org.gradle.jvmargs=-Xmx{heap_mb}m\norg.gradle.daemon=false\n",
        encoding="utf-8",
    )
    return target
