"""Architecture checks for the four top-level platform modules."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_MODULES = ("bench", "eval", "evolution")
REMOVED_ROOTS = (
    "configs",
    "data",
    "ckpt",
    "scripts",
    ".cache",
    ".secrets",
)


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    return imported


def test_backend_modules_do_not_depend_on_each_other_or_the_web_adapter() -> None:
    for module_name in BACKEND_MODULES:
        forbidden = ({*BACKEND_MODULES, "web_ui"} - {module_name})
        for path in (PROJECT_ROOT / module_name).rglob("*.py"):
            if "tests" in path.relative_to(PROJECT_ROOT / module_name).parts:
                continue
            imports = _top_level_imports(path)
            assert imports.isdisjoint(forbidden), (
                f"{path.relative_to(PROJECT_ROOT)} crosses its module seam: "
                f"{sorted(imports & forbidden)}"
            )


def test_old_mixed_roots_are_gone() -> None:
    for name in REMOVED_ROOTS:
        assert not (PROJECT_ROOT / name).exists(), f"misplaced root directory: {name}"


def test_module_specific_support_files_live_with_their_module() -> None:
    for module_name in (*BACKEND_MODULES, "web_ui"):
        assert (PROJECT_ROOT / module_name / "tests").is_dir()
    for module_name in BACKEND_MODULES:
        assert (PROJECT_ROOT / module_name / "docs").is_dir()

    assert not any((PROJECT_ROOT / "tests" / name).exists() for name in BACKEND_MODULES)
    assert not (PROJECT_ROOT / "tests" / "web_ui").exists()
    assert not (PROJECT_ROOT / "docs").exists()
    assert (PROJECT_ROOT / "evolution" / "mkdocs.yml").is_file()
    assert not (PROJECT_ROOT / "improve").exists()


def test_web_adapter_may_depend_on_each_backend_module() -> None:
    imports: set[str] = set()
    for path in (PROJECT_ROOT / "web_ui").rglob("*.py"):
        imports.update(_top_level_imports(path))
    assert set(BACKEND_MODULES).issubset(imports)
