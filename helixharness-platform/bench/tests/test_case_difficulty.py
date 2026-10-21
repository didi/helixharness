"""Public contract tests for Case difficulty derived from the frozen code delta."""

from __future__ import annotations

import subprocess
from pathlib import Path

from bench.case_difficulty import assess_case_difficulty


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "evaluation@example.invalid")
    _git(repository, "config", "user.name", "Evaluation Fixture")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    return repository, _commit(repository, "base")


def test_case_difficulty_uses_actual_production_diff_volume(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)

    (repository / "src").mkdir()
    (repository / "src" / "small.py").write_text("value = 1\n", encoding="utf-8")
    easy_commit = _commit(repository, "small change")
    easy = assess_case_difficulty(repository, base, easy_commit)

    assert easy.level == "EASY"
    assert easy.production_files == 1
    assert easy.production_lines == 1
    assert easy.evidence_text == "业务代码改动 1 行"

    for index in range(31):
        (repository / "src" / f"small_{index}.py").write_text(
            "value = 1\n",
            encoding="utf-8",
        )
    many_small_files_commit = _commit(repository, "many small files")
    many_small_files = assess_case_difficulty(
        repository,
        easy_commit,
        many_small_files_commit,
    )

    assert many_small_files.level == "EASY"
    assert many_small_files.production_files == 31
    assert many_small_files.production_lines == 31

    for index in range(4):
        (repository / "src" / f"medium_{index}.py").write_text(
            "\n".join(f"value_{line} = {line}" for line in range(30)) + "\n",
            encoding="utf-8",
        )
    medium_commit = _commit(repository, "medium change")
    medium = assess_case_difficulty(
        repository,
        many_small_files_commit,
        medium_commit,
    )

    assert medium.level == "MEDIUM"
    assert medium.production_files == 4
    assert medium.production_lines == 120

    (repository / "src" / "large.py").write_text(
        "\n".join(f"value_{line} = {line}" for line in range(1001)) + "\n",
        encoding="utf-8",
    )
    hard_commit = _commit(repository, "large change")
    hard = assess_case_difficulty(repository, medium_commit, hard_commit)

    assert hard.level == "HARD"
    assert hard.production_files == 1
    assert hard.production_lines == 1001


def test_tests_docs_and_configuration_are_not_business_code(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "docs").mkdir()
    (repository / ".github" / "workflows").mkdir(parents=True)
    (repository / "src" / "main" / "resources" / "mapper").mkdir(parents=True)
    (repository / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    for index in range(20):
        (repository / "tests" / f"test_{index}.py").write_text(
            "\n".join("assert True" for _ in range(100)) + "\n",
            encoding="utf-8",
        )
    (repository / "docs" / "design.md").write_text("design\n" * 500, encoding="utf-8")
    (repository / "pom.xml").write_text("<project />\n" * 500, encoding="utf-8")
    (repository / "application.yml").write_text("flag: true\n" * 500, encoding="utf-8")
    (repository / ".github" / "workflows" / "ci.yml").write_text(
        "run: test\n" * 500,
        encoding="utf-8",
    )
    (repository / "src" / "main" / "resources" / "mapper" / "OrderMapper.xml").write_text(
        "<select />\n",
        encoding="utf-8",
    )
    solution = _commit(repository, "feature with extensive evidence")

    assessment = assess_case_difficulty(repository, base, solution)

    assert assessment.level == "EASY"
    assert assessment.production_files == 2
    assert assessment.production_lines == 2
    assert assessment.supporting_files == 24
    assert assessment.supporting_lines == 4000
