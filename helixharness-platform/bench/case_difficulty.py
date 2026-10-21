"""Derive an operator-facing Case difficulty from its frozen business-code delta."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class CaseDifficultyAssessment:
    """A reproducible size band plus the measurements that support it."""

    level: str
    production_files: int
    production_lines: int
    supporting_files: int
    supporting_lines: int

    @property
    def evidence_text(self) -> str:
        return f"业务代码改动 {self.production_lines} 行"


UNASSESSED_DIFFICULTY = CaseDifficultyAssessment(
    level="UNASSESSED",
    production_files=0,
    production_lines=0,
    supporting_files=0,
    supporting_lines=0,
)


def assess_case_difficulty(
    repository: Path,
    base_commit: str,
    solution_commit: str,
) -> CaseDifficultyAssessment:
    """Measure Base-to-Solution business-code changes and assign a size band."""

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "diff",
            "--numstat",
            base_commit,
            solution_commit,
            "--",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    production_files = production_lines = 0
    supporting_files = supporting_lines = 0
    for row in completed.stdout.splitlines():
        fields = row.split("\t", 2)
        if len(fields) != 3:
            continue
        added, deleted, raw_path = fields
        changed_lines = (
            int(added) + int(deleted)
            if added.isdigit() and deleted.isdigit()
            else 0
        )
        if _is_supporting_path(raw_path):
            supporting_files += 1
            supporting_lines += changed_lines
        else:
            production_files += 1
            production_lines += changed_lines

    if production_lines > 1_000:
        level = "HARD"
    elif production_lines <= 100:
        level = "EASY"
    else:
        level = "MEDIUM"
    return CaseDifficultyAssessment(
        level=level,
        production_files=production_files,
        production_lines=production_lines,
        supporting_files=supporting_files,
        supporting_lines=supporting_lines,
    )


def _is_supporting_path(raw_path: str) -> bool:
    path = PurePosixPath(raw_path.lower())
    parts = set(path.parts)
    name = path.name
    if (
        bool(
            parts
            & {
                "test",
                "tests",
                "__tests__",
                "docs",
                "doc",
                "documentation",
                "generated",
                "generated-sources",
            }
        )
        or name.startswith("test_")
        or name.endswith(
            (
                "test.java",
                "tests.java",
                "test.kt",
                "tests.kt",
                "_test.go",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
            )
        )
    ):
        return True
    if name.endswith(".bpmn20.xml"):
        return False
    if path.suffix == ".xml":
        return "mapper" not in parts and not name.endswith("mapper.xml")
    return path.suffix not in {
        ".java",
        ".kt",
        ".scala",
        ".groovy",
        ".go",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".svelte",
        ".rb",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".m",
        ".mm",
        ".sql",
        ".proto",
        ".thrift",
        ".graphql",
        ".gql",
        ".bpmn",
    }
