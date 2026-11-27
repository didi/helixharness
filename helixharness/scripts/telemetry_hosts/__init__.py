"""Host adapters owned exclusively by Lite telemetry."""

from __future__ import annotations

import re
import importlib
import json
import sys
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


HOST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RUNTIME_SNAPSHOT_NAME = "telemetry-hosts.json"
RUNTIME_SNAPSHOT_SCHEMA = "helixharness-telemetry-hosts/1.0"
_ADAPTER_PATTERN = re.compile(r"^python:(telemetry_hosts\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")


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


@dataclass(frozen=True)
class _RuntimeManifest:
    host_id: str
    aliases: tuple[str, ...]
    adapter: str

    def capability(self, name: str) -> str:
        return "emulated" if name == "telemetry" else "unsupported"

    def load_python_adapter(self, name: str):
        if name != "telemetry":
            raise ValueError("runtime snapshot contains telemetry adapters only")
        match = _ADAPTER_PATTERN.fullmatch(self.adapter)
        value = importlib.import_module(match.group(1))
        for part in match.group(2).split("."):
            value = getattr(value, part)
        return value


class _RuntimeRegistry:
    def __init__(self, payload: Any):
        if (not isinstance(payload, dict) or set(payload) != {"schema", "hosts"}
                or payload.get("schema") != RUNTIME_SNAPSHOT_SCHEMA or not isinstance(payload.get("hosts"), list)):
            raise ValueError("invalid runtime telemetry snapshot")
        self._manifests: dict[str, _RuntimeManifest] = {}
        self._identities: dict[str, str] = {}
        for item in payload["hosts"]:
            if not isinstance(item, dict) or set(item) != {"host_id", "aliases", "adapter"}:
                raise ValueError("invalid runtime telemetry host")
            host_id, aliases, adapter = item["host_id"], item["aliases"], item["adapter"]
            if (not isinstance(host_id, str) or not HOST_ID_PATTERN.fullmatch(host_id)
                    or not isinstance(aliases, list) or not isinstance(adapter, str) or not _ADAPTER_PATTERN.fullmatch(adapter)):
                raise ValueError("invalid runtime telemetry host contract")
            for identity in [host_id, *aliases]:
                if not isinstance(identity, str) or not HOST_ID_PATTERN.fullmatch(identity) or identity in self._identities:
                    raise ValueError("duplicate or invalid runtime telemetry identity")
                self._identities[identity] = host_id
            self._manifests[host_id] = _RuntimeManifest(host_id, tuple(aliases), adapter)

    def resolve_id(self, value: str) -> str | None:
        return self._identities.get(value)

    def get(self, value: str):
        return self._manifests.get(self.resolve_id(value))

    def manifests(self):
        return tuple(self._manifests[key] for key in sorted(self._manifests))


def build_runtime_snapshot(plugin_root: Path) -> dict[str, Any]:
    """Validate the selected installation, then export only telemetry contracts.

    Explicit repair may run from an installed runtime; it can load the caller's
    selected source to reinstall. Normal collection never loads that source.
    """
    scripts = str(plugin_root.resolve() / "scripts")
    sys.path.insert(0, scripts)
    try:
        from host_runtime import HostRegistry
        registry = HostRegistry.discover(plugin_root.resolve() / "hosts")
    finally:
        sys.path.remove(scripts)
    payload = {"schema": RUNTIME_SNAPSHOT_SCHEMA, "hosts": [
        {"host_id": manifest.host_id, "aliases": list(manifest.aliases), "adapter": manifest.adapters["telemetry"]}
        for manifest in registry.manifests() if manifest.capability("telemetry") != "unsupported"
    ]}
    _RuntimeRegistry(payload)
    return payload


@lru_cache(maxsize=1)
def _host_registry():
    snapshot = Path(__file__).resolve().parents[1] / RUNTIME_SNAPSHOT_NAME
    if snapshot.exists() or (PLUGIN_ROOT / "runtime-manifest.json").is_file():
        if snapshot.is_symlink():
            raise ValueError("runtime telemetry snapshot must be a regular owned file")
        return _RuntimeRegistry(json.loads(snapshot.read_text(encoding="utf-8")))
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
