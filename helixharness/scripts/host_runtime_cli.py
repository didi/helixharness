#!/usr/bin/env python3
"""输出宿主无关的 HelixHarness Host Context。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from host_runtime import HostContractError, HostManifest, HostRegistry
from source_binding import guard_source


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOSTS_ROOT = PLUGIN_ROOT / "hosts"
CONTEXT_SCHEMA_VERSION = "1.0"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _context(manifest: HostManifest) -> dict[str, Any]:
    resources: dict[str, str] = {}
    adapter_kinds: dict[str, str] = {}
    for name in sorted(manifest.adapters):
        reference = manifest.require_adapter_reference(name)
        adapter_kinds[name] = reference.kind
        if reference.kind == "resource":
            resource = manifest.resolve_resource_adapter(name)
            resources[name] = resource.relative_to(PLUGIN_ROOT.resolve()).as_posix()
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "status": "resolved",
        "host_id": manifest.host_id,
        "plugin_root": str(PLUGIN_ROOT.resolve()),
        "host_manifest": str((manifest.root / "host.json").resolve()),
        "resolved_resources": {key: str((PLUGIN_ROOT / path).resolve()) for key, path in resources.items()},
        "host_api_version": manifest.host_api_version,
        "capabilities": dict(sorted(manifest.capabilities.items())),
        "adapter_kinds": adapter_kinds,
        "resources": resources,
    }


def _resolve(registry: HostRegistry, explicit: str | None) -> HostManifest | None:
    requested = explicit or os.environ.get("HELIXHARNESS_PLATFORM")
    if requested:
        return registry.get(requested)
    detected = registry.detect(os.environ)
    return registry.get(detected) if detected else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="解析 HelixHarness Host Context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve", help="解析当前宿主")
    resolve.add_argument("--platform", help="显式指定宿主 ID 或别名")
    resolve.add_argument("--expected-plugin-root", type=Path, help="当前 SessionStart 绑定的插件根目录")
    subparsers.add_parser("list", help="列出已安装 Host Kit")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    guard_source(PLUGIN_ROOT, getattr(arguments, "expected_plugin_root", None))
    try:
        registry = HostRegistry.discover(HOSTS_ROOT)
        if arguments.command == "list":
            _emit(
                {
                    "schema_version": CONTEXT_SCHEMA_VERSION,
                    "status": "ok",
                    "hosts": list(registry.supported_ids()),
                }
            )
            return 0
        manifest = _resolve(registry, arguments.platform)
        if manifest is None:
            _emit(
                {
                    "schema_version": CONTEXT_SCHEMA_VERSION,
                    "status": "error",
                    "reason": "unsupported_host",
                    "message": "当前宿主未注册，且不会回退到其他宿主",
                }
            )
            return 2
        _emit(_context(manifest))
        return 0
    except HostContractError as exc:
        _emit(
            {
                "schema_version": CONTEXT_SCHEMA_VERSION,
                "status": "error",
                "reason": "invalid_host_contract",
                "message": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
