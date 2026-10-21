#!/usr/bin/env python3
"""Statically audit one Scenario v2 Case for known portability risks."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

_SHELL_FACTS = (
    re.compile(r"^(behavior|requirements?|contract) (is|are) frozen\.?$", re.I),
    re.compile(r"^(按|根据).*(需求|方案).*(实现|处理).*$"),
    re.compile(r"^(保持|保证).*(兼容|一致).*$"),
    re.compile(r"^行为(已经|已)?冻结[。.]?$"),
)
_WORKFLOW_FACT = re.compile(
    r"(review|archive|commit|push|归档|评审|"
    r"提交(?:代码|变更|并(?:推送|归档)|到.{0,20}(?:分支|仓库))|"
    r"推送|继续下一|workflow 模式)",
    re.I,
)
_PRIVATE_REFLECTION = re.compile(r"getDeclaredField\s*\(|setAccessible\s*\(\s*true\s*\)")
_SOURCE_TEXT_ASSERTION = re.compile(
    r"(readString|readAllBytes|Files\.read|源码|source).{0,100}(contains|matches|assert|断言)",
    re.I | re.S,
)
_F2P_NAMES = re.compile(r"(f2p|fail[_-]?to[_-]?pass)", re.I)
_P2P_NAMES = re.compile(r"(p2p|pass[_-]?to[_-]?pass)", re.I)
_HISTORICAL_TEST_QUOTA = re.compile(
    r"(?:必须|要求|验收|复跑).{0,60}(?:真实|历史).{0,20}\d+\s*项.{0,20}测试",
    re.I | re.S,
)
_PLATFORM_CALIBRATION_EVIDENCE = re.compile(
    r"(?:Base\s*(?:与|和|/)|Solution|F2P|P2P|hidden|隐藏).{0,140}"
    r"(?:PASS|通过|JUnit|XML|测试|报告|证据|校准)|"
    r"(?:PASS|通过|JUnit|XML|测试|报告|证据|校准).{0,140}"
    r"(?:Base\s*(?:与|和|/)|Solution|F2P|P2P|hidden|隐藏)",
    re.I | re.S,
)
_CANDIDATE_VISIBILITY_BOUNDARY = re.compile(
    r"(?:不属于候选|不会暴露给候选|评测平台.{0,100}独立|"
    r"不得把.{0,140}(?:Candidate|候选).{0,80}(?:验收|材料|证据|补齐)|"
    r"缺少.{0,140}不是.{0,80}(?:Review|Verify|Archive))",
    re.I | re.S,
)
_NON_PRODUCTION_PREFIXES = ("docs/", "test/", "tests/", "src/test/", "src/it/")


def _finding(code: str, severity: str, message: str, path: str | None = None) -> dict[str, str]:
    result = {"code": code, "severity": severity, "message": message}
    if path:
        result["path"] = path
    return result


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 顶层必须是对象")
    return payload


def _declared_path(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 必须是非空相对路径")
    path = (root / value).resolve()
    if root != path and root not in path.parents:
        raise ValueError(f"{field} 逃逸 Case Source：{value}")
    return path


def _production_change_lines(repository: Path, definition: dict[str, Any]) -> int:
    base = str(definition.get("base_commit", ""))
    solution = str(definition.get("solution_commit", ""))
    completed = subprocess.run(
        ["git", "-C", str(repository.resolve()), "diff", "--numstat", f"{base}..{solution}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed_lines = 0
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        normalized = path.lower().lstrip("./")
        if normalized.startswith(_NON_PRODUCTION_PREFIXES) or "/src/test/" in normalized:
            continue
        if added == "-" or deleted == "-":
            changed_lines += 1
        else:
            changed_lines += int(added) + int(deleted)
    return changed_lines


def audit(
    case_dir: Path,
    repository: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    root = case_dir.expanduser().resolve()
    findings: list[dict[str, str]] = []
    manifest = _read_json(root / "case.json")
    gold_path = _declared_path(root, manifest.get("gold_path", "gold.json"), "gold_path")
    policy_path = _declared_path(
        root,
        manifest.get("clarification_policy_path", "clarification-policy.json"),
        "clarification_policy_path",
    )
    gold = _read_json(gold_path)
    policy = _read_json(policy_path)

    initial_request_path = _declared_path(
        root,
        manifest.get("initial_request_path", "initial-request.md"),
        "initial_request_path",
    )
    initial_request = initial_request_path.read_text(encoding="utf-8")
    if _HISTORICAL_TEST_QUOTA.search(initial_request):
        findings.append(
            _finding(
                "UNREPRODUCIBLE_HISTORICAL_TEST_REQUIREMENT",
                "RISK",
                "强制历史测试数量必须同时提供 Base 可见的测试身份、命令和可复现入口",
                str(initial_request_path),
            )
        )
    if (
        _PLATFORM_CALIBRATION_EVIDENCE.search(initial_request)
        and not _CANDIDATE_VISIBILITY_BOUNDARY.search(initial_request)
    ):
        findings.append(
            _finding(
                "CANDIDATE_INVISIBLE_CALIBRATION_REQUIREMENT",
                "RISK",
                "Initial Request 引用了平台 Base/Solution 或隐藏校准证据，但未声明候选可见性边界",
                str(initial_request_path),
            )
        )

    production_change_lines: int | None = None
    if manifest.get("case_kind") == "REPOSITORY_CASE" and repository is not None:
        from repository_interval import repository_definitions

        definitions = repository_definitions(manifest)
        if not isinstance(repository, Mapping):
            raise ValueError("质量审计必须按 repository_id 绑定仓库")
        bindings = repository
        production_change_lines = 0
        for definition in definitions:
            repository_id = str(definition.get("repository_id", ""))
            local_repository = bindings.get(repository_id)
            if local_repository is None:
                raise ValueError(f"缺少本地仓库绑定：{repository_id}")
            production_change_lines += _production_change_lines(local_repository, definition)
        if production_change_lines == 0:
            findings.append(
                _finding(
                    "ZERO_PRODUCTION_CODE_CHANGE",
                    "BLOCKER",
                    "Base/Solution 区间没有生产代码改动，不应生成 Case",
                )
            )

    if "hidden_checks" in gold:
        findings.append(_finding("RETIRED_HIDDEN_CHECKS", "BLOCKER", "V2 Gold 不允许 hidden_checks", str(gold_path)))

    facts = policy.get("facts")
    if not isinstance(facts, list) or not facts:
        findings.append(_finding("EMPTY_CLARIFICATION_FACTS", "BLOCKER", "澄清策略没有冻结业务事实", str(policy_path)))
    else:
        for index, fact in enumerate(facts):
            statement = fact.get("statement") if isinstance(fact, dict) else None
            source = fact.get("source") if isinstance(fact, dict) else None
            location = f"{policy_path}#facts[{index}]"
            if not isinstance(statement, str) or not statement.strip():
                findings.append(_finding("EMPTY_CLARIFICATION_STATEMENT", "BLOCKER", "业务事实 statement 为空", location))
                continue
            normalized = statement.strip()
            if any(pattern.search(normalized) for pattern in _SHELL_FACTS):
                findings.append(_finding("SHELL_CLARIFICATION_FACT", "BLOCKER", "业务事实是无边界答案的空壳表述", location))
            if _WORKFLOW_FACT.search(normalized):
                findings.append(_finding("WORKFLOW_CHOICE_AS_FACT", "BLOCKER", "流程选择不得伪装成业务事实", location))
            if not isinstance(source, str) or not source.strip():
                findings.append(_finding("UNSOURCED_CLARIFICATION_FACT", "RISK", "业务事实没有可追溯 source", location))

    text_files: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > 2_000_000
            or any(part in {".git", ".env", "__pycache__", ".cache"} for part in path.parts)
        ):
            continue
        try:
            text_files.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue

    for path, content in text_files:
        relative = str(path.relative_to(root))
        if _PRIVATE_REFLECTION.search(content):
            findings.append(_finding("PRIVATE_IMPLEMENTATION_COUPLING", "RISK", "隐藏资产可能绑定私有字段或反射形态", relative))
        if _SOURCE_TEXT_ASSERTION.search(content):
            findings.append(_finding("SOURCE_TEXT_ASSERTION", "RISK", "可能以源码文本代替可观察行为断言", relative))
        if path.suffix.lower() in {".sh", ".py", ".java", ".kt", ".groovy", ".patch"} and (
            _P2P_NAMES.search(relative + "\n" + content) and _F2P_NAMES.search(content)
        ):
            findings.append(_finding("P2P_F2P_BUILD_COUPLING", "RISK", "P2P 资产可能引用或编译 F2P 专用内容", relative))

    kinds = {
        item.get("kind")
        for item in gold.get("execution_tests", [])
        if isinstance(item, dict)
    }
    if kinds != {"FAIL_TO_PASS", "PASS_TO_PASS"}:
        findings.append(_finding("INCOMPLETE_EXECUTION_MATRIX", "BLOCKER", "execution_tests 必须同时包含 F2P 与 P2P", str(gold_path)))

    deduplicated = {
        (item["code"], item.get("path", ""), item["message"]): item for item in findings
    }
    unique = list(deduplicated.values())
    unique.sort(key=lambda item: (item["severity"] != "BLOCKER", item["code"], item.get("path", "")))
    blockers = sum(item["severity"] == "BLOCKER" for item in unique)
    risks = sum(item["severity"] == "RISK" for item in unique)
    return {
        "schema_version": "helixharness-case-creator-quality-audit.v1",
        "case_id": manifest.get("case_id"),
        "decision": "BLOCKED" if blockers else "NEEDS_REVIEW" if risks else "STATIC_CHECK_PASSED",
        "blocker_count": blockers,
        "risk_count": risks,
        "production_change_lines": production_change_lines,
        "findings": unique,
        "note": "静态通过不替代 Base/Solution/合法替代实现三方重放或正式 Run。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        result = audit(arguments.case_dir, repositories or None)
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({"decision": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["decision"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
