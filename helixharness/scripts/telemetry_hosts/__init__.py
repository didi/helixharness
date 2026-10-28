"""Host adapters owned exclusively by Lite telemetry."""

from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


HOST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass
class SupplementWindow:
    """Normalized host events consumed by the Lite DLP pipeline."""

    events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class HostAdapter(Protocol):
    """The only host-specific interface visible to Lite telemetry."""

    host_id: str
    parser_version: str

    def consent_cache_path(self, override: str | None = None) -> Path: ...

    def current_session_context(self, repo: Path) -> dict[str, str] | None: ...

    def validate_session_source(
        self, value: Any, repo: Path | None = None
    ) -> str | None: ...

    def collect_supplement_window(
        self,
        source: Path,
        *,
        started_at: str,
        ended_at: str,
        max_bytes: int | None = None,
    ) -> SupplementWindow: ...


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _host_registry():
    from host_runtime import HostRegistry

    return HostRegistry.discover(PLUGIN_ROOT / "hosts")


def normalize_host_id(value: Any, *, strict: bool = True) -> str:
    normalized = str(value or "").strip().lower()
    if not HOST_ID_PATTERN.fullmatch(normalized):
        if strict:
            raise ValueError("invalid telemetry host id")
        return ""
    # Only explicit aliases in installed manifests are accepted. Substrings,
    # model names and historical platform-family guesses are not host identity.
    return _host_registry().resolve_id(normalized) or normalized


@lru_cache(maxsize=None)
def _adapter(host_id: str) -> HostAdapter | None:
    manifest = _host_registry().get(host_id)
    if manifest is None or manifest.capability("telemetry") == "unsupported":
        return None
    adapter = manifest.load_python_adapter("telemetry")()
    if adapter.host_id != manifest.host_id:
        raise ValueError("telemetry adapter host identity mismatch")
    for method in (
        "consent_cache_path",
        "current_session_context",
        "validate_session_source",
        "collect_supplement_window",
    ):
        if not callable(getattr(adapter, method, None)):
            raise ValueError(f"telemetry adapter missing method: {method}")
    return adapter


def get_host_adapter(host_id: Any) -> HostAdapter | None:
    canonical = normalize_host_id(host_id, strict=False)
    return _adapter(canonical) if canonical else None


def supported_host_ids() -> tuple[str, ...]:
    return tuple(
        manifest.host_id
        for manifest in _host_registry().manifests()
        if manifest.capability("telemetry") != "unsupported"
    )
