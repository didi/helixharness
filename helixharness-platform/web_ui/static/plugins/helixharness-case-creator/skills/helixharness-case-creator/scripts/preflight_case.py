#!/usr/bin/env python3
"""Run HelixHarness Platform Case Source Preflight without registry, publication, or network writes."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Mapping

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError(f"Case Source 不允许符号链接：{root}")


def _bootstrap(platform_root: Path) -> None:
    root = platform_root.expanduser().resolve()
    if not (root / "bench" / "scenarios.py").is_file():
        raise ValueError(f"不是有效 HelixHarness Platform 根目录：{root}")
    sys.path.insert(0, str(root))


def preflight(
    platform_root: Path,
    case_dir: Path,
    repository: Mapping[str, Path] | None,
) -> dict[str, object]:
    _bootstrap(platform_root)
    from repository_interval import validate_repository_interval  # noqa: PLC0415

    from bench.scenarios import load_scenario_source  # noqa: PLC0415

    original_directory = case_dir.expanduser().resolve()
    if not (original_directory / "case.json").is_file():
        raise ValueError(f"Case Source 缺少 case.json：{original_directory}")
    _reject_symlinks(original_directory)
    with tempfile.TemporaryDirectory(prefix="helixharness-case-preflight-") as temporary_directory:
        source_directory = Path(temporary_directory) / original_directory.name
        shutil.copytree(original_directory, source_directory, symlinks=True)
        manifest_path = source_directory / "case.json"
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw_manifest, dict):
            raise ValueError("case.json 顶层必须是对象")
        source = load_scenario_source(source_directory)
        validate_repository_interval(source.manifest, repository)
        result: dict[str, object] = {
            "schema_version": "helixharness-case-creator-preflight.v2",
            "case_id": source.case_id,
            "passed": True,
            "checks": [
                {
                    "check_id": "SCENARIO_V2_CONTRACT",
                    "passed": True,
                    "evidence": "Case、Gold、澄清策略及所有声明文件通过 Scenario v2 确定性校验",
                }
            ],
            "source_hash": source.snapshot.source_hash,
        }
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", required=True, type=Path)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument(
        "--repository",
        action="append",
        help="可重复的 repository_id=/checkout 仓库绑定",
    )
    arguments = parser.parse_args()
    try:
        from repository_interval import parse_repositories

        repositories = parse_repositories(arguments.repository)
        repository: Mapping[str, Path] | None = repositories or None
        result = preflight(arguments.platform_root, arguments.case_dir, repository)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
