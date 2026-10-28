#!/usr/bin/env python3
"""Generate/check Codex command entries and non-command method projections."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Sequence


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ProjectionError(ValueError):
    """Raised when canonical skills cannot be projected safely."""


def _frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ProjectionError(f"skill frontmatter is missing: {path}")
    end = text.index("\n---\n", 4) + len("\n---\n")
    return text[:end]


def _frontmatter_value(path: Path, key: str) -> str:
    """Read the small YAML scalar subset used by Skill frontmatter."""

    lines = _frontmatter(path).splitlines()[1:-1]
    matches = [
        (index, line.split(":", 1)[1].strip())
        for index, line in enumerate(lines)
        if not line.startswith((" ", "\t")) and line.startswith(f"{key}:")
    ]
    if len(matches) != 1:
        raise ProjectionError(f"skill {key} must be unique: {path}")

    index, raw = matches[0]
    if raw in {">", ">-", ">+", "|", "|-", "|+"}:
        block: list[str] = []
        for line in lines[index + 1 :]:
            if line and not line.startswith((" ", "\t")):
                break
            block.append(line.strip())
        value = "\n".join(block) if raw.startswith("|") else " ".join(block)
    elif raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProjectionError(f"skill {key} has invalid JSON quoting: {path}") from exc
    elif raw.startswith("'") and raw.endswith("'"):
        value = raw[1:-1].replace("''", "'")
    else:
        value = raw

    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"skill {key} must be non-empty: {path}")
    return re.sub(r"\s+", " ", value).strip()


def _frontmatter_bool(path: Path, key: str, *, default: bool = False) -> bool:
    lines = _frontmatter(path).splitlines()[1:-1]
    values = [
        line.split(":", 1)[1].strip().lower()
        for line in lines
        if not line.startswith((" ", "\t")) and line.startswith(f"{key}:")
    ]
    if not values:
        return default
    if len(values) != 1 or values[0] not in {"true", "false"}:
        raise ProjectionError(f"skill {key} must be a unique boolean: {path}")
    return values[0] == "true"


def _method_skill_frontmatter(path: Path, name: str) -> str:
    canonical_name = _frontmatter_value(path, "name")
    if canonical_name != name:
        raise ProjectionError(
            f"skill name does not match its directory: {canonical_name} != {name}"
        )
    description = _frontmatter_value(path, "description")
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n"
    )


def _short_description(description: str) -> str:
    if len(description) <= 64:
        return description
    for separator in ("。", ". ", "；", "; "):
        end = description.find(separator)
        if 24 <= end < 64:
            return description[: end + len(separator)].strip()
    return description[:61].rstrip() + "..."


def _openai_metadata(path: Path, name: str) -> str:
    description = _frontmatter_value(path, "description")
    allow_implicit = not _frontmatter_bool(
        path, "disable-model-invocation", default=False
    )
    return (
        "interface:\n"
        f"  display_name: {json.dumps(f'helix:{name}', ensure_ascii=False)}\n"
        "  short_description: "
        f"{json.dumps(_short_description(description), ensure_ascii=False)}\n"
        "policy:\n"
        f"  allow_implicit_invocation: {str(allow_implicit).lower()}\n"
    )


def _command_skill_frontmatter(path: Path, name: str) -> str:
    """Project a canonical command description onto its Codex Skill surface."""

    frontmatter = _frontmatter(path)
    descriptions = [
        line.split(":", 1)[1].strip()
        for line in frontmatter.splitlines()
        if line.startswith("description:")
    ]
    if len(descriptions) != 1:
        raise ProjectionError(f"command description must be unique: {path}")
    try:
        description = json.loads(descriptions[0])
    except json.JSONDecodeError as exc:
        raise ProjectionError(f"command description must be JSON quoted: {path}") from exc
    if not isinstance(description, str) or not description.strip():
        raise ProjectionError(f"command description must be non-empty: {path}")
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "triggers:\n"
        f"  - /helix:{name}\n"
        "---\n"
    )


def projected_methods(plugin_root: Path) -> dict[Path, str]:
    root = plugin_root.resolve()
    projection = json.loads(
        (root / "hosts/codex/command-projection.json").read_text(encoding="utf-8")
    )
    entrypoints = projection.get("entrypoints")
    if not isinstance(entrypoints, dict):
        raise ProjectionError("Codex command projection is invalid")
    command_names = set(entrypoints)
    result: dict[Path, str] = {}
    for canonical in sorted((root / "skills").glob("*/SKILL.md")):
        name = canonical.parent.name
        if name in command_names:
            continue
        target = root / "hosts/codex/skills" / name / "SKILL.md"
        body = (
            _method_skill_frontmatter(canonical, name)
            + f"\n# Codex method projection: {name}\n\n"
            + "插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/codex/host.json`、"
            + "`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest "
            + "绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 "
            + "`reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex "
            + "适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill "
            + f"`skills/{name}/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。"
            + "canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 "
            + "Host Context 映射。\n"
        )
        result[target] = body
    return result


def _command_targets(plugin_root: Path) -> dict[Path, str]:
    root = plugin_root.resolve()
    projection = json.loads(
        (root / "hosts/codex/command-projection.json").read_text(encoding="utf-8")
    )
    result: dict[Path, str] = {}
    for name in projection["entrypoints"]:
        canonical = root / "commands" / f"{name}.md"
        target = root / "hosts/codex/skills" / name / "SKILL.md"
        if not target.is_file():
            raise ProjectionError(f"Codex command projection is missing: {name}")
        body = target.read_text(encoding="utf-8")
        body_end = body.index("\n---\n", 4) + len("\n---\n")
        result[target] = _command_skill_frontmatter(canonical, name) + body[body_end:]
    return result


def projected_openai_metadata(plugin_root: Path) -> dict[Path, str]:
    """Project canonical invocation intent onto Codex-native Skill metadata."""

    root = plugin_root.resolve()
    result: dict[Path, str] = {}
    for target in projected_methods(root):
        name = target.parent.name
        canonical = root / "skills" / name / "SKILL.md"
        metadata = target.parent / "agents/openai.yaml"
        result[metadata] = _openai_metadata(canonical, name)
    for target in _command_targets(root):
        name = target.parent.name
        canonical = root / "commands" / f"{name}.md"
        metadata = target.parent / "agents/openai.yaml"
        result[metadata] = _openai_metadata(canonical, name)
    return result


def write(plugin_root: Path) -> None:
    for path, content in _command_targets(plugin_root).items():
        path.write_text(content, encoding="utf-8")
    for path, content in projected_methods(plugin_root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for path, content in projected_openai_metadata(plugin_root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def check(plugin_root: Path) -> None:
    expected = projected_methods(plugin_root)
    for path, content in expected.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ProjectionError(f"Codex method projection is stale: {path.parent.name}")
    for path, content in _command_targets(plugin_root).items():
        if path.read_text(encoding="utf-8") != content:
            raise ProjectionError(
                f"Codex command frontmatter projection is stale: {path.parent.name}"
            )
    metadata = projected_openai_metadata(plugin_root)
    for path, content in metadata.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ProjectionError(
                "Codex OpenAI metadata projection is stale: "
                f"{path.parents[1].name}"
            )
    actual_metadata = set(
        (plugin_root.resolve() / "hosts/codex/skills").glob("*/agents/openai.yaml")
    )
    unexpected = actual_metadata - set(metadata)
    if unexpected:
        names = ", ".join(sorted(path.parents[1].name for path in unexpected))
        raise ProjectionError(f"unexpected Codex OpenAI metadata projection: {names}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PLUGIN_ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        write(args.root) if args.write else check(args.root)
    except (OSError, ValueError, TypeError, ProjectionError) as exc:
        print(f"Codex skill projection: FAIL: {exc}")
        return 1
    print("Codex skill projection: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
