#!/usr/bin/env python3
"""Prepare and page a deterministic Codex command entry without oversized output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from source_binding import guard_source
from typing import Any, Sequence

SCHEMA_VERSION = "1.1"
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_PACKET_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_PAGE_CONTENT_JSON_BYTES = 11 * 1024
DEFAULT_TTL_SECONDS = 15 * 60
STATE_OWNER_NAME = ".helixharness-owner"
STATE_OWNER_VALUE = "helixharness-codex-entry-state-v1\n"
INVOCATION_OWNER_NAME = ".helixharness-invocation"
COMMAND_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
INVOCATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STAGING_PATTERN = re.compile(r"^\.staging-[0-9a-f]{32}$")
CORE_PROTOCOLS = (
    "reference/workflow-projection-protocol.md",
    "reference/host-runtime-protocol.md",
    "reference/tool-semantics.md",
)
SHARED_ADAPTER_PROTOCOLS = {
    "subagent_protocol": "reference/subagent-runtime-protocol.md",
    "telemetry_protocol": "reference/telemetry-command-protocol.md",
}
CORE_ADAPTER_KEYS = frozenset({"command_projection", "runtime"})


class EntryLoadError(ValueError):
    """Raised when a Codex entry cannot be loaded deterministically."""


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise EntryLoadError(f"invalid JSON resource: {path.name}") from exc
    if not isinstance(value, dict):
        raise EntryLoadError(f"invalid JSON object: {path.name}")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise EntryLoadError("invalid resource path")
    return value


def _resource_path(root: Path, relative: str) -> Path:
    canonical = _safe_relative(relative)
    path = root / canonical
    if path.is_symlink():
        raise EntryLoadError(f"resource must not be a symlink: {canonical}")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise EntryLoadError("resource escapes plugin root") from exc
    if not resolved.is_file():
        raise EntryLoadError(f"resource is missing: {canonical}")
    return resolved


def _adapter_relative(host: dict[str, Any], key: str) -> str:
    adapters = host.get("adapters")
    reference = adapters.get(key) if isinstance(adapters, dict) else None
    if not isinstance(reference, str) or not reference.startswith("resource:"):
        raise EntryLoadError(f"adapter is not a resource: {key}")
    return _safe_relative(reference.removeprefix("resource:"))


def _command_relative(root: Path, projection_relative: str, command: str) -> str:
    projection = _object(_resource_path(root, projection_relative))
    entrypoints = projection.get("entrypoints")
    entry = entrypoints.get(command) if isinstance(entrypoints, dict) else None
    expected = f"commands/{command}.md"
    expected_entry = f"hosts/codex/skills/{command}/SKILL.md"
    if (
        not isinstance(entry, list)
        or entry != [expected_entry, expected]
        or any(not isinstance(item, str) for item in entry)
    ):
        raise EntryLoadError("command projection is missing or ambiguous")
    return expected


def _read_resource(root: Path, relative: str) -> tuple[bytes, str]:
    content = _resource_path(root, relative).read_bytes()
    if len(content) > MAX_RESOURCE_BYTES:
        raise EntryLoadError(f"resource exceeds bounded packet: {relative}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EntryLoadError(f"resource is not UTF-8: {relative}") from exc
    if any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        raise EntryLoadError(f"resource contains unsupported control byte: {relative}")
    return content, text


def _selected_entry(
    plugin_root: Path,
    *,
    command: str,
    resources: Sequence[str],
) -> tuple[Path, list[tuple[int, list[tuple[str, bytes, str]]]]]:
    if not COMMAND_PATTERN.fullmatch(command):
        raise EntryLoadError("invalid command")
    root = plugin_root.resolve()
    host_relative = "hosts/codex/host.json"
    host = _object(_resource_path(root, host_relative))
    if host.get("host_id") != "codex" or host.get("host_api_version") != "1.1":
        raise EntryLoadError("Codex Host Context is invalid")
    if len(resources) != len(set(resources)):
        raise EntryLoadError("adapter resource keys must be unique")

    projection = _adapter_relative(host, "command_projection")
    command_relative = _command_relative(root, projection, command)
    declaration_paths = [command_relative]
    declaration_text = "\n".join(
        _resource_path(root, relative).read_text(encoding="utf-8")
        for relative in declaration_paths
    )
    declared = set(re.findall(r"resources\.([a-z_]+)", declaration_text)) - CORE_ADAPTER_KEYS
    resource_adapters = {
        key
        for key in declared
        if isinstance(host.get("adapters"), dict)
        and isinstance(host["adapters"].get(key), str)
        and host["adapters"][key].startswith("resource:")
    }
    unexpected = set(resources) - resource_adapters
    if unexpected:
        raise EntryLoadError(f"adapter is not allowed for command: {sorted(unexpected)[0]}")

    core_selected = list(
        dict.fromkeys(
            (*CORE_PROTOCOLS, host_relative, _adapter_relative(host, "runtime"), projection, command_relative)
        )
    )
    adapter_selected: list[str] = []
    for key in resources:
        shared = SHARED_ADAPTER_PROTOCOLS.get(key)
        if shared is not None:
            adapter_selected.append(shared)
        adapter_selected.append(_adapter_relative(host, key))
        if key == "subagent_protocol":
            adapter_selected.append("hosts/shared/subagent-result-encoding.md")

    packets: list[tuple[int, list[tuple[str, bytes, str]]]] = []
    for packet_round, selected in ((1, core_selected), (2, list(dict.fromkeys(adapter_selected)))):
        if not selected:
            continue
        entries: list[tuple[str, bytes, str]] = []
        total = 0
        for relative in selected:
            content, text = _read_resource(root, relative)
            total += len(content)
            if total > MAX_PACKET_BYTES:
                raise EntryLoadError("entry packet exceeds bounded output")
            entries.append((relative, content, text))
        packets.append((packet_round, entries))
    return root, packets


def _default_state_root() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"helixharness-codex-entry-state-v1-{uid}"


def _secure_state_root(value: Path | None) -> Path:
    root = value if value is not None else _default_state_root()
    if root.exists() and root.is_symlink():
        raise EntryLoadError("entry state root must not be a symlink")
    if root.exists() and not root.is_dir():
        raise EntryLoadError("entry state root is invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = root / STATE_OWNER_NAME
    if not owner.exists() and not owner.is_symlink():
        foreign = [
            entry
            for entry in root.iterdir()
            if entry.name != STATE_OWNER_NAME
            and not entry.name.startswith(f"{STATE_OWNER_NAME}.tmp-")
        ]
        if foreign:
            raise EntryLoadError("entry state root is not owned by HelixHarness")
        temporary_owner = root / f"{STATE_OWNER_NAME}.tmp-{secrets.token_hex(8)}"
        temporary_owner.write_text(STATE_OWNER_VALUE, encoding="utf-8")
        temporary_owner.chmod(0o600)
        temporary_owner.replace(owner)
    if owner.is_symlink() or not owner.is_file():
        raise EntryLoadError("entry state root is not owned by HelixHarness")
    try:
        owner_value = owner.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EntryLoadError("entry state root is not owned by HelixHarness") from exc
    if owner_value != STATE_OWNER_VALUE:
        raise EntryLoadError("entry state root is not owned by HelixHarness")
    root.chmod(0o700)
    resolved = root.resolve()
    if not resolved.is_dir():
        raise EntryLoadError("entry state root is invalid")
    return resolved


def _invocation_dir(state_root: Path, invocation_key: str) -> Path:
    if not INVOCATION_PATTERN.fullmatch(invocation_key):
        raise EntryLoadError("invalid invocation key")
    path = state_root / invocation_key
    if path.is_symlink():
        raise EntryLoadError("entry state must not be a symlink")
    try:
        path.resolve().relative_to(state_root)
    except ValueError as exc:
        raise EntryLoadError("entry state escapes state root") from exc
    return path


def _cleanup_expired(state_root: Path, now: float) -> None:
    for child in state_root.iterdir():
        is_invocation = INVOCATION_PATTERN.fullmatch(child.name) is not None
        is_staging = STAGING_PATTERN.fullmatch(child.name) is not None
        if not (is_invocation or is_staging) or child.is_symlink() or not child.is_dir():
            continue
        invocation_owner = child / INVOCATION_OWNER_NAME
        try:
            owned = (
                not invocation_owner.is_symlink()
                and invocation_owner.read_text(encoding="utf-8") == STATE_OWNER_VALUE
            )
        except (OSError, UnicodeError):
            owned = False
        if not owned:
            continue
        try:
            old_enough = now - child.stat().st_mtime > DEFAULT_TTL_SECONDS
        except OSError:
            continue
        if is_staging:
            if old_enough:
                shutil.rmtree(child)
            continue
        try:
            manifest = _object(child / "manifest.json")
            expired = float(manifest.get("expires_at", 0)) <= now
        except (EntryLoadError, OSError, TypeError, ValueError):
            expired = old_enough
        if expired:
            shutil.rmtree(child)


def _chunk_text(text: str) -> list[str]:
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while remaining:
        low, high = 1, len(remaining)
        selected = 1
        while low <= high:
            middle = (low + high) // 2
            size = len(json.dumps(remaining[:middle], ensure_ascii=False).encode("utf-8"))
            if size <= MAX_PAGE_CONTENT_JSON_BYTES:
                selected = middle
                low = middle + 1
            else:
                high = middle - 1
        newline = remaining.rfind("\n", 0, selected)
        if newline >= max(1, selected // 2):
            selected = newline + 1
        chunks.append(remaining[:selected])
        remaining = remaining[selected:]
    return chunks


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) >= MAX_RESPONSE_BYTES:
        raise EntryLoadError("loader response exceeds bounded output")
    return value


def _write_state_manifest(invocation: Path, manifest: dict[str, Any]) -> None:
    temporary_manifest = invocation / "manifest.json.tmp"
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary_manifest.chmod(0o600)
    temporary_manifest.replace(invocation / "manifest.json")


def prepare_entry(
    plugin_root: Path,
    *,
    command: str,
    resources: Sequence[str] = (),
    state_root: Path | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    if ttl_seconds <= 0:
        raise EntryLoadError("entry TTL must be positive")
    root, selected_packets = _selected_entry(plugin_root, command=command, resources=resources)
    now = time.time()
    secure_root = _secure_state_root(state_root)
    _cleanup_expired(secure_root, now)
    for _attempt in range(3):
        invocation_key = secrets.token_hex(16)
        invocation = _invocation_dir(secure_root, invocation_key)
        staging = secure_root / f".staging-{invocation_key}"
        try:
            staging.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise EntryLoadError("could not allocate entry state")
    invocation_owner = staging / INVOCATION_OWNER_NAME
    invocation_owner.write_text(STATE_OWNER_VALUE, encoding="utf-8")
    invocation_owner.chmod(0o600)
    response_packets: list[dict[str, Any]] = []
    stored_pages: dict[str, list[dict[str, Any]]] = {}
    try:
        for packet_round, entries in selected_packets:
            packet_pages: list[dict[str, Any]] = []
            response_resources: list[dict[str, Any]] = []
            for relative, content, text in entries:
                chunks = _chunk_text(text)
                source_sha256 = hashlib.sha256(content).hexdigest()
                response_resources.append(
                    {"path": relative, "source_sha256": source_sha256, "bytes": len(content), "page_count": len(chunks)}
                )
                for resource_page_index, chunk in enumerate(chunks):
                    page_index = len(packet_pages)
                    filename = f"page-{packet_round}-{page_index}.txt"
                    page_path = staging / filename
                    page_path.write_text(chunk, encoding="utf-8")
                    page_path.chmod(0o400)
                    page_bytes = chunk.encode("utf-8")
                    packet_pages.append(
                        {
                            "filename": filename,
                            "path": relative,
                            "source_sha256": source_sha256,
                            "page_sha256": hashlib.sha256(page_bytes).hexdigest(),
                            "resource_page_index": resource_page_index,
                            "resource_page_count": len(chunks),
                        }
                    )
            stored_pages[str(packet_round)] = packet_pages
            response_packets.append({"round": packet_round, "page_count": len(packet_pages), "resources": response_resources})

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "plugin_root": str(root),
            "host_id": "codex",
            "canonical_host_id": "codex",
            "command": command,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "pages": stored_pages,
            "progress": {packet_round: 0 for packet_round in stored_pages},
        }
        _write_state_manifest(staging, manifest)
        result = _bounded(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "ready",
                "host_id": "codex",
                "canonical_host_id": "codex",
                "command": command,
                "max_rounds": 2,
                "invocation_key": invocation_key,
                "expires_at": int(now + ttl_seconds),
                "packets": response_packets,
            }
        )
        staging.replace(invocation)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _state_manifest(
    plugin_root: Path,
    *,
    invocation_key: str,
    state_root: Path | None,
) -> tuple[Path, dict[str, Any]]:
    secure_root = _secure_state_root(state_root)
    invocation = _invocation_dir(secure_root, invocation_key)
    if not invocation.is_dir():
        raise EntryLoadError("entry state is missing or expired")
    invocation_owner = invocation / INVOCATION_OWNER_NAME
    try:
        owned = (
            not invocation_owner.is_symlink()
            and invocation_owner.read_text(encoding="utf-8") == STATE_OWNER_VALUE
        )
    except (OSError, UnicodeError):
        owned = False
    if not owned:
        raise EntryLoadError("entry state is not owned by HelixHarness")
    manifest = _object(invocation / "manifest.json")
    if manifest.get("plugin_root") != str(plugin_root.resolve()):
        raise EntryLoadError("entry state belongs to a different plugin root")
    if float(manifest.get("expires_at", 0)) <= time.time():
        shutil.rmtree(invocation)
        raise EntryLoadError("entry state is missing or expired")
    return invocation, manifest


def read_entry_page(
    plugin_root: Path,
    *,
    invocation_key: str,
    packet_round: int,
    page_index: int,
    state_root: Path | None = None,
) -> dict[str, Any]:
    invocation, manifest = _state_manifest(plugin_root, invocation_key=invocation_key, state_root=state_root)
    pages = manifest.get("pages", {}).get(str(packet_round))
    if not isinstance(pages, list) or page_index < 0 or page_index >= len(pages):
        raise EntryLoadError("entry page is missing or out of order")
    all_pages = manifest.get("pages")
    progress = manifest.get("progress")
    if not isinstance(all_pages, dict) or not isinstance(progress, dict):
        raise EntryLoadError("entry page metadata is invalid")
    next_page = progress.get(str(packet_round))
    if not isinstance(next_page, int) or page_index > next_page:
        raise EntryLoadError("entry page is missing or out of order")
    if packet_round == 2:
        first_pages = all_pages.get("1")
        first_progress = progress.get("1")
        if not isinstance(first_pages, list) or first_progress != len(first_pages):
            raise EntryLoadError("entry page is missing or out of order")
    metadata = pages[page_index]
    if not isinstance(metadata, dict):
        raise EntryLoadError("entry page metadata is invalid")
    filename = metadata.get("filename")
    if not isinstance(filename, str) or not re.fullmatch(r"page-[12]-[0-9]+\.txt", filename):
        raise EntryLoadError("entry page metadata is invalid")
    page_path = invocation / filename
    if page_path.is_symlink() or not page_path.is_file():
        raise EntryLoadError("entry page identity is invalid")
    content = page_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata.get("page_sha256"):
        raise EntryLoadError("entry page identity is invalid")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EntryLoadError("entry page is not UTF-8") from exc
    result = _bounded(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "page",
            "invocation_key": invocation_key,
            "command": manifest["command"],
            "round": packet_round,
            "page_index": page_index,
            "page_count": len(pages),
            "path": metadata["path"],
            "source_sha256": metadata["source_sha256"],
            "page_sha256": metadata["page_sha256"],
            "resource_page_index": metadata["resource_page_index"],
            "resource_page_count": metadata["resource_page_count"],
            "content": text,
        }
    )
    if page_index == next_page:
        progress[str(packet_round)] = next_page + 1
        _write_state_manifest(invocation, manifest)
    return result


def finish_entry(
    plugin_root: Path,
    *,
    invocation_key: str,
    state_root: Path | None = None,
) -> dict[str, Any]:
    invocation, manifest = _state_manifest(plugin_root, invocation_key=invocation_key, state_root=state_root)
    pages = manifest.get("pages")
    progress = manifest.get("progress")
    if not isinstance(pages, dict) or not isinstance(progress, dict) or any(
        not isinstance(packet_pages, list)
        or progress.get(packet_round) != len(packet_pages)
        for packet_round, packet_pages in pages.items()
    ):
        raise EntryLoadError("entry pages are not fully consumed")
    shutil.rmtree(invocation)
    return _bounded(
        {"schema_version": SCHEMA_VERSION, "status": "finished", "invocation_key": invocation_key, "command": manifest["command"]}
    )


def main(argv: Sequence[str] | None = None) -> int:
    guard_source(Path(__file__).resolve().parent.parent)
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--expected-plugin-root", type=Path)
    parser.add_argument("--command")
    parser.add_argument("--resource", action="append", default=[])
    parser.add_argument("--state-root", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--read-page", action="store_true")
    action.add_argument("--finish", action="store_true")
    parser.add_argument("--invocation-key")
    parser.add_argument("--packet-round", type=int)
    parser.add_argument("--page-index", type=int)
    args = parser.parse_args(argv)
    guard_source(args.plugin_root, args.expected_plugin_root)
    try:
        if args.read_page:
            if args.command or args.resource or not args.invocation_key or args.packet_round is None or args.page_index is None:
                raise EntryLoadError("invalid read-page arguments")
            result = read_entry_page(
                args.plugin_root,
                invocation_key=args.invocation_key,
                packet_round=args.packet_round,
                page_index=args.page_index,
                state_root=args.state_root,
            )
        elif args.finish:
            if args.command or args.resource or not args.invocation_key:
                raise EntryLoadError("invalid finish arguments")
            result = finish_entry(
                args.plugin_root,
                invocation_key=args.invocation_key,
                state_root=args.state_root,
            )
        else:
            if not args.command or args.invocation_key or args.packet_round is not None or args.page_index is not None:
                raise EntryLoadError("invalid prepare arguments")
            result = prepare_entry(args.plugin_root, command=args.command, resources=args.resource, state_root=args.state_root)
    except (EntryLoadError, OSError, UnicodeError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "code": "helixharness_host_contract_invalid", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
