#!/usr/bin/env python3
"""Generate/check pi command entry Skills under hosts/pi/skills."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from codex_skill_projection import _frontmatter


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = Path("hosts/pi/skills")


class ProjectionError(ValueError):
    """Raised when the pi command projection cannot be generated safely."""


def _description(contract: Path) -> str:
    """Read the JSON-quoted description from a canonical command contract."""

    values = [
        line.split(":", 1)[1].strip()
        for line in _frontmatter(contract).splitlines()
        if line.startswith("description:")
    ]
    if len(values) != 1:
        raise ProjectionError(f"command description must be unique: {contract}")
    try:
        description = json.loads(values[0])
    except json.JSONDecodeError as exc:
        raise ProjectionError(f"command description must be JSON quoted: {contract}") from exc
    if not isinstance(description, str) or not description.strip():
        raise ProjectionError(f"command description must be non-empty: {contract}")
    return description


def _body(command: str) -> str:
    return (
        f"\n# HelixHarness {command}\n\n"
        "插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/pi/host.json`、manifest 绑定的 "
        "`resources.runtime`、`resources.command_projection`、`reference/host-runtime-protocol.md` 和 "
        "`reference/tool-semantics.md`，并按 `reference/runtime-preflight.md` 执行一次\n"
        "`sh {plugin_root}/scripts/runtime_preflight.sh check`。\n\n"
        f"然后以唯一 `commands/{command}.md` 为命令契约，完整读取并按其中显式声明的 adapter 顺序执行。\n"
        "不得扫描 plugin cache，不得读取其他宿主的入口，也不得为该命令补充本文件之外的业务语义。\n"
    )


def projected(plugin_root: Path) -> dict[Path, str]:
    root = plugin_root.resolve()
    projection = json.loads(
        (root / "hosts/pi/command-projection.json").read_text(encoding="utf-8")
    )
    entrypoints = projection.get("entrypoints")
    if not isinstance(entrypoints, dict):
        raise ProjectionError("pi command projection is invalid")
    result: dict[Path, str] = {}
    for command, pair in sorted(entrypoints.items()):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ProjectionError(f"pi command projection entry is invalid: {command}")
        target = root / str(pair[0])
        contract = root / str(pair[1])
        if target.parent.name != f"helix-{command}":
            raise ProjectionError(f"pi command entry must be helix-{command}: {pair[0]}")
        result[target] = (
            "---\n"
            f"name: {target.parent.name}\n"
            f"description: {json.dumps(_description(contract), ensure_ascii=False)}\n"
            "---\n"
            f"{_body(command)}"
        )
    return result


def write(plugin_root: Path) -> None:
    for path, content in projected(plugin_root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def check(plugin_root: Path) -> None:
    expected = projected(plugin_root)
    for path, content in expected.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise ProjectionError(f"pi command projection is stale: {path.parent.name}")
    actual = {
        path
        for path in (plugin_root.resolve() / SKILLS_ROOT).glob("*/SKILL.md")
    }
    unexpected = actual - set(expected)
    if unexpected:
        names = ", ".join(sorted(path.parent.name for path in unexpected))
        raise ProjectionError(f"unexpected pi command projection: {names}")


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
        print(f"Pi skill projection: FAIL: {exc}")
        return 1
    print("Pi skill projection: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
