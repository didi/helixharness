#!/usr/bin/env python3
"""Build the Qoder plugin archive from this plugin root.

Qoder loads one manifest (``.qoder-plugin/plugin.json``) and the convention directories
``agents/``, ``skills/`` and ``hooks/hooks.json``. Measured on the host, a plugin's
``commands/`` directory never becomes a slash command, so the public workflows are projected
as thin skill entries under ``hosts/qoder/skills/<command>/`` and published through the
manifest's ``skills`` declaration. Everything that belongs to another host stays out of the
archive, and the two host-specific adjustments Qoder needs are applied to the archive only:

* ``hosts/qoder/hooks.json`` is published as the root ``hooks/hooks.json``, because Qoder only
  reads hooks from that fixed path.
* ``agents/*.md`` gain a ``name`` key matching the file stem, which Qoder uses to register the
  subagent role. Shared sources are never rewritten for one host.

Usage::

    python3 scripts/package_qoder_plugin.py [--output DIR]

Only the surfaces Qoder loads are packaged. The allowlist below is positive: a
new top-level directory or file must be added deliberately. Untracked files in
a git checkout and symlinks that resolve outside the plugin root are refused,
so local state cannot leak into a distributed archive.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from cli_stdio import configure_utf8_stdio

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE = PurePosixPath(".qoder-plugin/plugin.json")
HOST_RELATIVE = PurePosixPath("hosts/qoder/host.json")
HOOKS_SOURCE_RELATIVE = PurePosixPath("hosts/qoder/hooks.json")
HOOKS_TARGET_RELATIVE = PurePosixPath("hooks/hooks.json")
QODER_SKILL_ENTRY_ROOT = PurePosixPath("hosts/qoder/skills")

# Owned by other hosts, or not part of the Qoder surface at all.
EXCLUDED_TOP_LEVEL = {".claude-plugin", ".codex-plugin", "SKILL.md", "dist"}
EXCLUDED_PATHS = {PurePosixPath("hosts/claude-code"), PurePosixPath("hosts/codex")}
EXCLUDED_NAMES = {"__pycache__", ".DS_Store", ".git", ".ruff_cache", ".pytest_cache", "output"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".coverage"}

# Positive allowlist: only Qoder-loadable surfaces ship.
ALLOWED_TOP_LEVEL_DIRS = {
    "agents", "commands", "hosts", "reference", "scripts", "skills", "templates", "workflows",
}
ALLOWED_TOP_LEVEL_FILES = {
    "LICENSE", "NOTICE", "PLUGIN_USAGE.md", "README.md", "THIRD_PARTY_NOTICES.md",
}
ALLOWED_HOST_CHILDREN = {"qoder", "shared", "host-manifest-v1.schema.json"}
ALLOWED_PLUGIN_DIRS = {".qoder-plugin"}
GENERATED_ARCHIVE_PATHS = {PurePosixPath("hooks/hooks.json")}

ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


class PackagingError(RuntimeError):
    """Raised when the archive would not be loadable by Qoder."""


def _read_manifest_text(plugin_root: Path, relative: PurePosixPath) -> str:
    path = plugin_root / str(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackagingError(f"缺少插件文件: {relative}") from exc


def plugin_identity(plugin_root: Path) -> tuple[str, str]:
    try:
        payload = json.loads(_read_manifest_text(plugin_root, MANIFEST_RELATIVE))
    except json.JSONDecodeError as exc:
        raise PackagingError(f"清单无法解析: {MANIFEST_RELATIVE}") from exc
    if not isinstance(payload, dict):
        raise PackagingError("清单必须是 JSON 对象")
    name = str(payload.get("name") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not name or not version:
        raise PackagingError("清单必须声明 name 与 version")
    return name, version


def declared_skill_roots(plugin_root: Path) -> tuple[str, ...]:
    """Return the plugin-relative skill directories the Qoder manifest declares."""

    payload = json.loads(_read_manifest_text(plugin_root, MANIFEST_RELATIVE))
    raw = payload.get("skills")
    values = raw if isinstance(raw, list) else [raw]
    roots: list[str] = []
    for value in values:
        text = str(value or "").strip().removeprefix("./").rstrip("/")
        if text and text not in roots:
            roots.append(text)
    return tuple(roots)


def adapter_resource_path(plugin_root: Path, name: str) -> PurePosixPath:
    """Return the plugin-relative file a resource adapter points at."""

    for key, relative in _adapter_entries(plugin_root):
        if key == name:
            return relative
    raise PackagingError(f"宿主清单未声明资源适配器: {name}")


def _adapter_entries(plugin_root: Path) -> list[tuple[str, PurePosixPath]]:
    try:
        payload = json.loads(_read_manifest_text(plugin_root, HOST_RELATIVE))
    except json.JSONDecodeError as exc:
        raise PackagingError(f"清单无法解析: {HOST_RELATIVE}") from exc
    adapters = payload.get("adapters")
    if not isinstance(adapters, dict):
        raise PackagingError("宿主清单必须声明 adapters")
    entries: list[tuple[str, PurePosixPath]] = []
    for name, raw in sorted(adapters.items()):
        reference = str(raw or "")
        kind, _, target = reference.partition(":")
        if kind != "resource":
            # Python adapters are optional for Qoder; a non-resource reference is out of
            # scope for this archive and the host contract loader rejects it separately.
            continue
        relative = PurePosixPath(target)
        if relative.is_absolute() or ".." in relative.parts:
            raise PackagingError(f"适配器资源越过插件根: {name}")
        entries.append((name, relative))
    if not entries:
        raise PackagingError("宿主清单没有声明任何资源适配器")
    return entries


def host_resource_paths(plugin_root: Path) -> list[PurePosixPath]:
    """Return every resource file the Qoder host manifest declares."""

    return [relative for _, relative in _adapter_entries(plugin_root)]


def _frontmatter(text: str) -> tuple[list[str], list[str]] | None:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[: index + 1], lines[index + 1 :]
    return None


def agent_with_name(text: str, agent_name: str) -> str:
    """Insert a ``name`` key when the shared agent file does not declare one."""

    split = _frontmatter(text)
    if split is None:
        raise PackagingError(f"agent 缺少 YAML frontmatter: {agent_name}")
    header, body = split
    for line in header[1:-1]:
        stripped = line.strip()
        if stripped.startswith("name:"):
            declared = stripped[len("name:") :].strip().strip("\"'")
            if declared != agent_name:
                raise PackagingError(
                    f"agent frontmatter name 与文件名不一致: {declared} != {agent_name}"
                )
            return text
    header.insert(1, f"name: {agent_name}\n")
    return "".join(header) + "".join(body)


def skill_directories(plugin_root: Path, relative_root: str = "skills") -> list[Path]:
    skills_root = plugin_root / relative_root
    if not skills_root.is_dir():
        return []
    return sorted(path for path in skills_root.iterdir() if (path / "SKILL.md").is_file())


def validate_plugin(plugin_root: Path) -> None:
    """Run the static checks Qoder itself would apply, before writing anything."""

    plugin_identity(plugin_root)
    for relative in host_resource_paths(plugin_root):
        if not (plugin_root / str(relative)).is_file():
            raise PackagingError(f"宿主适配器资源不存在: {relative}")
    if not (plugin_root / str(HOOKS_SOURCE_RELATIVE)).is_file():
        raise PackagingError(f"缺少宿主 hook 源: {HOOKS_SOURCE_RELATIVE}")
    try:
        hooks = json.loads(_read_manifest_text(plugin_root, HOOKS_SOURCE_RELATIVE))
    except json.JSONDecodeError as exc:
        raise PackagingError(f"hook 清单无法解析: {HOOKS_SOURCE_RELATIVE}") from exc
    if not isinstance(hooks.get("hooks"), dict) or not hooks["hooks"]:
        raise PackagingError("hook 清单必须声明非空 hooks 对象")
    entry_relative = adapter_resource_path(plugin_root, "command_projection")
    payload = json.loads(_read_manifest_text(plugin_root, entry_relative))
    entrypoints = payload.get("entrypoints") or {}
    if payload.get("surface") != "skill":
        raise PackagingError("Qoder 投影必须使用 skill surface：宿主没有把插件 commands/ 注册为斜杠命令面")
    for command, pair in sorted(entrypoints.items()):
        entry = PurePosixPath(str(pair[0]))
        if entry != QODER_SKILL_ENTRY_ROOT / command / "SKILL.md":
            raise PackagingError(f"命令薄入口路径不符合投影约定: {command}")
        if str(pair[1]) != f"commands/{command}.md":
            raise PackagingError(f"命令契约路径不是唯一公共契约: {command}")
        if not (plugin_root / str(entry)).is_file():
            raise PackagingError(f"命令入口不存在: {command}")
    # An undeclared skill root ships files Qoder never loads, so the entry would satisfy
    # the host contract while staying invisible as a slash command.
    if str(QODER_SKILL_ENTRY_ROOT) not in set(declared_skill_roots(plugin_root)):
        raise PackagingError(f"清单未声明薄入口目录: {QODER_SKILL_ENTRY_ROOT}")
    for skill_root in ("skills", str(QODER_SKILL_ENTRY_ROOT)):
        for skill in skill_directories(plugin_root, skill_root):
            split = _frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
            if split is None:
                raise PackagingError(f"Skill 缺少 frontmatter: {skill.name}")
            header = "".join(split[0])
            if f"name: {skill.name}" not in header:
                raise PackagingError(f"Skill frontmatter name 与目录名不一致: {skill.name}")


def is_excluded(relative: PurePosixPath) -> bool:
    if relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    for excluded in EXCLUDED_PATHS:
        if relative == excluded or relative.parts[: len(excluded.parts)] == excluded.parts:
            return True
    if any(part in EXCLUDED_NAMES for part in relative.parts):
        return True
    return relative.suffix in EXCLUDED_SUFFIXES


def is_packaged(relative: PurePosixPath) -> bool:
    """Return True when a plugin-relative path belongs in the Qoder archive."""
    parts = relative.parts
    if not parts:
        return False
    top = parts[0]
    if top in ALLOWED_PLUGIN_DIRS:
        return len(parts) > 1 and not is_excluded(relative)
    if top == "hosts":
        if len(parts) < 2 or parts[1] not in ALLOWED_HOST_CHILDREN:
            return False
        return not is_excluded(relative)
    if top in ALLOWED_TOP_LEVEL_DIRS:
        return not is_excluded(relative)
    return len(parts) == 1 and top in ALLOWED_TOP_LEVEL_FILES


def _git_untracked(plugin_root: Path) -> set[PurePosixPath] | None:
    """Return unversioned files under the plugin root, or None outside git.

    An installed plugin cache is not a git checkout; the allowlist still bounds
    the archive there, but provenance cannot be checked.
    """
    command = ["git", "-C", str(plugin_root), "ls-files", "--others", "-z"]
    try:
        result = subprocess.run(command, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return {
        PurePosixPath(entry)
        for entry in result.stdout.decode("utf-8", "surrogateescape").split("\0")
        if entry
    }


def collect_entries(plugin_root: Path) -> dict[PurePosixPath, bytes]:
    root = plugin_root.resolve()
    untracked = _git_untracked(plugin_root)
    unversioned: list[str] = []
    archive: dict[PurePosixPath, bytes] = {}
    for path in sorted(plugin_root.rglob("*")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(plugin_root).as_posix())
        if not is_packaged(relative):
            continue
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise PackagingError(f"符号链接越过插件根: {relative} -> {path.resolve()}")
        if untracked is not None and relative in untracked:
            unversioned.append(str(relative))
            continue
        archive[relative] = path.read_bytes()
    if unversioned:
        raise PackagingError(
            f"未纳入版本控制的 {len(unversioned)} 个文件会被打包: "
            + ", ".join(sorted(unversioned)[:5])
        )

    archive[HOOKS_TARGET_RELATIVE] = _read_manifest_text(
        plugin_root, HOOKS_SOURCE_RELATIVE
    ).encode("utf-8")

    for relative in [item for item in archive if item.match("agents/*.md")]:
        agent_name = relative.name[:-len(".md")]
        text = archive[relative].decode("utf-8")
        archive[relative] = agent_with_name(text, agent_name).encode("utf-8")

    if MANIFEST_RELATIVE not in archive:
        raise PackagingError("归档根目录缺少插件清单")
    return archive


def write_archive(plugin_root: Path, output_dir: Path) -> Path:
    validate_plugin(plugin_root)
    name, version = plugin_identity(plugin_root)
    archive = collect_entries(plugin_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{name}-{version}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in sorted(archive):
            info = zipfile.ZipInfo(str(relative), date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = stat.S_IMODE(0o644) << 16
            bundle.writestr(info, archive[relative])
    payload = buffer.getvalue()
    target.write_bytes(payload)
    return target


def verify_archive(plugin_root: Path, archive_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as bundle:
        names = [PurePosixPath(item.filename) for item in bundle.infolist()]
        if MANIFEST_RELATIVE not in names:
            raise PackagingError("ZIP 根目录缺少 .qoder-plugin/plugin.json")
        for relative in names:
            if relative.is_absolute() or ".." in relative.parts:
                raise PackagingError(f"ZIP 条目路径不安全: {relative}")
            if relative not in GENERATED_ARCHIVE_PATHS and not is_packaged(relative):
                raise PackagingError(f"ZIP 混入非 Qoder 插件文件: {relative}")
        for relative in host_resource_paths(plugin_root):
            if relative not in names:
                raise PackagingError(f"ZIP 缺少宿主适配器资源: {relative}")
        if HOOKS_TARGET_RELATIVE not in names:
            raise PackagingError("ZIP 缺少根 hooks/hooks.json")
        projection = json.loads(
            _read_manifest_text(plugin_root, adapter_resource_path(plugin_root, "command_projection"))
        )
        entrypoints = projection.get("entrypoints") or {}
        for command, pair in sorted(entrypoints.items()):
            for reference in (str(pair[0]), str(pair[1])):
                if PurePosixPath(reference) not in names:
                    raise PackagingError(f"ZIP 缺少命令入口文件: {command} -> {reference}")
        agents = [item for item in names if item.match("agents/*.md")]
        for relative in agents:
            text = bundle.read(str(relative)).decode("utf-8")
            if not _frontmatter(text) or not any(
                line.strip().startswith("name:") for line in _frontmatter(text)[0]
            ):
                raise PackagingError(f"ZIP 内 agent 缺少 name: {relative}")
    return {
        "entries": len(names),
        "agents": len(agents),
        "bytes": archive_path.stat().st_size,
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Package this plugin for Qoder.")
    parser.add_argument(
        "--output",
        default=str(PLUGIN_ROOT / "dist"),
        help="Directory for the generated archive (default: <plugin root>/dist).",
    )
    arguments = parser.parse_args(argv)
    archive_path = write_archive(PLUGIN_ROOT, Path(arguments.output).expanduser().resolve())
    summary = verify_archive(PLUGIN_ROOT, archive_path)
    print(json.dumps({"archive": str(archive_path), **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
