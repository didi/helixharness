#!/usr/bin/env python3
"""Generate and verify Cursor skill entries bound to canonical HelixHarness sources."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Sequence
from cli_stdio import configure_utf8_stdio


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COMMANDS = frozenset({"init", "propose", "apply", "archive", "bug-fix", "ut", "update", "status", "config"})
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ProjectionError(ValueError):
    """A source or generated entry violates the Cursor projection contract."""


def _inside(root: Path, relative: str, *, source: bool = True) -> Path:
    path = root / relative
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ProjectionError(f"projection path escapes plugin root: {relative}") from exc
    if source and not path.is_file():
        raise ProjectionError(f"projection source is missing: {relative}")
    return path


def _fields(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ProjectionError(f"source frontmatter is missing: {path.name}")
    lines = text[4:text.index("\n---\n", 4)].splitlines()
    fields: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(name|description|disable-model-invocation):\s*(.*)$", line)
        if not match:
            continue
        key, raw = match.groups()
        if key in fields:
            raise ProjectionError(f"duplicate source frontmatter field: {key}")
        if raw in {">", ">-", ">+", "|", "|-", "|+"}:
            block: list[str] = []
            for continuation in lines[index + 1:]:
                if continuation and not continuation.startswith((" ", "\t")):
                    break
                block.append(continuation.strip())
            value = " ".join(block)
        elif raw.startswith('"'):
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise ProjectionError(f"invalid source frontmatter quoting: {key}") from exc
        elif raw.startswith("'") and raw.endswith("'"):
            value = raw[1:-1].replace("''", "'")
        else:
            value = raw
        if not isinstance(value, str) or not value.strip():
            raise ProjectionError(f"empty source frontmatter field: {key}")
        fields[key] = re.sub(r"\s+", " ", value).strip()
    if "description" not in fields:
        raise ProjectionError("source description is missing")
    if fields.get("disable-model-invocation", "false") not in {"true", "false"}:
        raise ProjectionError("disable-model-invocation must be a boolean")
    return fields


def _frontmatter(name: str, fields: dict[str, str], *, command: bool) -> str:
    disabled = "true" if command else fields.get("disable-model-invocation", "false")
    return (
        "---\n"
        f"name: helix-{name}\n"
        f"description: {json.dumps(fields['description'], ensure_ascii=False)}\n"
        f"disable-model-invocation: {disabled}\n"
        "---\n\n"
    )


def _command_projection(root: Path) -> dict:
    projection_path = _inside(root, "hosts/cursor/command-projection.json")
    try:
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ProjectionError("invalid Cursor command projection") from exc
    if (
        not isinstance(projection, dict)
        or set(projection) != {"projection_version", "surface", "entrypoints"}
        or projection.get("projection_version") != "1.0"
        or projection.get("surface") != "skill"
        or not isinstance(projection.get("entrypoints"), dict)
        or set(projection["entrypoints"]) != COMMANDS
    ):
        raise ProjectionError("Cursor projection must cover exactly nine canonical commands")
    return projection


def resolve_entry(plugin_root: Path, entry: Path) -> dict[str, str]:
    """Resolve commands and methods independently without loading another entry type."""
    root = plugin_root.resolve()
    candidate = entry if entry.is_absolute() else root / entry
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise ProjectionError("Cursor entry escapes plugin root") from exc
    parts = relative.parts
    if len(parts) != 5 or parts[:3] != ("hosts", "cursor", "skills") or parts[-1] != "SKILL.md" or not parts[3].startswith("helix-"):
        raise ProjectionError("invalid Cursor entry path")
    name = parts[3][len("helix-"):]
    if not NAME_PATTERN.fullmatch(name):
        raise ProjectionError("invalid Cursor entry name")
    source = _inside(root, relative.as_posix())
    if _fields(source).get("name") != f"helix-{name}":
        raise ProjectionError("Cursor entry name does not match its path")
    if name in COMMANDS:
        kind = "command"
        canonical = f"commands/{name}.md"
        pair = _command_projection(root)["entrypoints"][name]
        if pair != [relative.as_posix(), canonical]:
            raise ProjectionError("Cursor command projection redirects its entry or contract")
        command_source = _inside(root, canonical)
        if "HelixHarness 公共命令唯一契约" not in command_source.read_text(encoding="utf-8"):
            raise ProjectionError("canonical command marker is missing")
    else:
        # Methods deliberately do not consult command-projection.json or commands/.
        kind = "method"
        canonical = f"skills/{name}/SKILL.md"
        if _fields(_inside(root, canonical)).get("name") != name:
            raise ProjectionError("canonical method name does not match its path")
    return {"entry_kind": kind, "name": name, "entry": relative.as_posix(), "canonical": canonical}


def projected_skills(plugin_root: Path) -> dict[Path, str]:
    """Build only thin entries; canonical business instructions remain single-source."""
    root = plugin_root.resolve()
    projection = _command_projection(root)
    result: dict[Path, str] = {}
    for name in sorted(COMMANDS):
        canonical = f"commands/{name}.md"
        target = f"hosts/cursor/skills/helix-{name}/SKILL.md"
        if projection["entrypoints"][name] != [target, canonical]:
            raise ProjectionError(f"Cursor command source or entry is invalid: {name}")
        source = _inside(root, canonical)
        if "HelixHarness 公共命令唯一契约" not in source.read_text(encoding="utf-8"):
            raise ProjectionError(f"canonical command marker is missing: {name}")
        result[_inside(root, target, source=False)] = (
            _frontmatter(name, _fields(source), command=True)
            + f"# Cursor command projection: {name}\n\n"
            + "入口类型：`command`。插件根固定为当前文件的 `../../../..`。完整读取 "
            + "`hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“命令入口”分支；"
            + "完成 runtime preflight，以 `--platform cursor` 显式解析同一安装的 Host Context。\n\n"
            + f"当前逻辑命令为 `{name}`，原生 Skill 名为 `helix-{name}`。按入口协议绑定本次用户参数为 "
            + f"`$ARGUMENTS`，然后完整读取并执行唯一公共契约 `{canonical}`；"
            + "不复制业务步骤，不重新发现插件，不把建议命令当成自动授权。\n"
        )

    for source in sorted((root / "skills").glob("*/SKILL.md")):
        name = source.parent.name
        if not NAME_PATTERN.fullmatch(name) or name in COMMANDS:
            raise ProjectionError(f"invalid or colliding canonical method name: {name}")
        source = _inside(root, f"skills/{name}/SKILL.md")
        fields = _fields(source)
        if fields.get("name") != name:
            raise ProjectionError(f"canonical method name differs from directory: {name}")
        content = (
            _frontmatter(name, fields, command=False)
            + f"# Cursor method projection: {name}\n\n"
            + "入口类型：`method`。插件根固定为当前文件的 `../../../..`。完整读取 "
            + "`hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“方法入口”分支；"
            + "不查询命令投影、不加载 commands 文件、不调用 Router。"
            + "执行 runtime preflight 并以 `--platform cursor` 解析同一安装的 Host Context。"
            + "完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 "
            + "`resources.runtime`；需要子 agent 时再读取公共子代理协议及 `resources.subagent_protocol`。\n\n"
            + f"完整读取并执行唯一 canonical 方法 `skills/{name}/SKILL.md` 的正文。"
            + "canonical frontmatter 不属于 Cursor 元数据，不装载其中的 Claude hooks、工具声明或调用标记。"
            + f"正文相对资源从 `skills/{name}/` 解析；不得改以投影目录为基准。"
            + "保留实际步骤、输出工件与全部门禁，不复制或改写业务语义，不扫描全局同名 Skill。\n"
        )
        if name == "d-d2c":
            content += (
                "\nD2C 必须执行 mode 对应的完整 canonical workflow、实际 runtime 健康与截图联合检查、"
                "fidelity 评测和 phase 6。新 phase 0 后记录本次 `pageDir` 与 `enteredAt`；"
                "在任何用户询问或结束前执行 `resources.runtime` 规定的 "
                "`hosts/cursor/d2c_gate.cjs`，分别使用 `--action ask` 或 `--action stop`。"
                "只有退出码 0 且 `allow=true` 可询问/结束；只有 `action.kind=done` 可宣称完成。"
                "不把缺少原生 skill hook 当作省略门禁的理由。\n"
            )
        result[_inside(root, f"hosts/cursor/skills/helix-{name}/SKILL.md", source=False)] = content
    return result


def write(plugin_root: Path) -> None:
    for target, content in projected_skills(plugin_root).items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def check(plugin_root: Path) -> None:
    expected = projected_skills(plugin_root)
    for target, content in expected.items():
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            raise ProjectionError(f"Cursor projection is missing or stale: {target.parent.name}")
    actual = set((plugin_root.resolve() / "hosts/cursor/skills").rglob("SKILL.md"))
    if actual != set(expected):
        raise ProjectionError("unexpected Cursor skill projection")


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PLUGIN_ROOT)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--write", action="store_true")
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--resolve-entry", type=Path, metavar="SKILL", help="Resolve one installed command or method entry")
    arguments = parser.parse_args(argv)
    try:
        if arguments.resolve_entry is not None:
            print(json.dumps(resolve_entry(arguments.root, arguments.resolve_entry), ensure_ascii=False, sort_keys=True))
        else:
            (write if arguments.write else check)(arguments.root)
    except (ProjectionError, OSError, UnicodeError) as exc:
        parser.exit(2, f"Cursor projection error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
