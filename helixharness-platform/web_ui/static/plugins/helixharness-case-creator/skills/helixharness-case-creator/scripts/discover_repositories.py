#!/usr/bin/env python3
"""Discover committed completed-feature candidates below one local directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SKIP_DIRECTORIES = {
    ".cache",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
REQUIRED_STAGE_DOCUMENTS = {
    "spec.md",
    "tasks.md",
    "verify-report.md",
}
_LFS_MARKER = "version https://git-lfs.github.com/spec/v1"


def _git_command(
    repository: Path,
    *arguments: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> tuple[str, str | None]:
    label = "git " + " ".join(arguments[:3])
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, UnicodeError) as exc:
        return "", f"{label}: 无法执行：{exc}"
    except subprocess.TimeoutExpired:
        return "", f"{label}: 30 秒超时"
    if completed.returncode not in allowed_returncodes:
        return "", f"{label}: 退出码 {completed.returncode}"
    return completed.stdout.strip(), None


def _repository_roots(root: Path, max_depth: int) -> tuple[list[Path], list[str], bool]:
    roots: list[Path] = []
    scan_errors: list[str] = []
    depth_limit_reached = False

    def record_error(error: OSError) -> None:
        scan_errors.append(f"{error.filename or root}: {error.strerror or error}")

    for current_text, directory_names, _ in os.walk(
        root,
        topdown=True,
        onerror=record_error,
        followlinks=False,
    ):
        current = Path(current_text)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            directory_names[:] = []
            continue
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in SKIP_DIRECTORIES and not (current / name).is_symlink()
        )
        if depth > max_depth:
            directory_names[:] = []
            continue
        top_level, git_error = _git_command(current, "rev-parse", "--show-toplevel")
        if top_level and Path(top_level).resolve() == current.resolve():
            roots.append(current.resolve())
            directory_names[:] = []
            continue
        if (current / ".git").exists() and git_error:
            roots.append(current.resolve())
            scan_errors.append(f"{current}: repository detection: {git_error}")
            directory_names[:] = []
            continue
        if depth == max_depth and directory_names:
            depth_limit_reached = True
            directory_names[:] = []
    return sorted(set(roots)), sorted(set(scan_errors)), depth_limit_reached


def _head_paths(tree_output: str) -> list[str]:
    return sorted(
        path
        for line in tree_output.splitlines()
        if (separator := line.find("\t")) >= 0 and (path := line[separator + 1 :])
    )


def _build_system(head_paths: set[str]) -> str:
    if any(path in head_paths for path in ("gradlew", "build.gradle", "build.gradle.kts")):
        return "GRADLE"
    if any(path in head_paths for path in ("mvnw", "pom.xml")):
        return "MAVEN"
    if any(path in head_paths for path in ("pyproject.toml", "requirements.txt")):
        return "PYTHON"
    if "package.json" in head_paths:
        return "NODE"
    return "UNKNOWN"


def _submodule_paths(content: str) -> list[str]:
    return sorted(
        match.group(1).strip()
        for line in content.splitlines()
        if (match := re.match(r"\s*path\s*=\s*(.+?)\s*$", line))
    )


def _gitlink_paths(tree_output: str) -> list[str]:
    paths: list[str] = []
    for line in tree_output.splitlines():
        metadata, separator, path = line.partition("\t")
        if separator and metadata.startswith("160000 commit "):
            paths.append(path)
    return sorted(paths)


def _lfs_pointer_paths(output: str) -> list[str]:
    return sorted(line.removeprefix("HEAD:") for line in output.splitlines() if line)


def _sanitize_remote(remote: str) -> str:
    """Remove credentials and query values before including a remote in JSON."""

    value = remote.strip()
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "ssh"}:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        if parsed.scheme == "ssh" and parsed.username:
            netloc = f"{parsed.username}@{netloc}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return re.sub(r"^[^/@\s]+@", "", value).split("?", 1)[0].split("#", 1)[0]


def _canonical_remote(remote: str) -> str:
    return _sanitize_remote(remote).strip().rstrip("/")


def _features(
    repository: Path,
    head_paths: list[str],
    *,
    head: str,
    repository_blockers: list[str],
) -> list[dict[str, Any]]:
    prefix = "docs/specs/completed/"
    grouped: dict[str, list[str]] = {}
    for path in head_paths:
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix) :]
        feature, separator, relative_path = remainder.partition("/")
        if not separator or not feature or not relative_path:
            continue
        grouped.setdefault(feature, []).append(relative_path)

    result: list[dict[str, Any]] = []
    for feature, files in sorted(grouped.items()):
        top_level_files = {path for path in files if "/" not in path}
        missing_documents = sorted(REQUIRED_STAGE_DOCUMENTS - top_level_files)
        blockers = list(repository_blockers)
        if "spec.md" not in top_level_files:
            blockers.append("MISSING_SPEC")
        if missing_documents:
            blockers.append("MISSING_STAGE_DOCUMENTS")
        if "MISSING_SPEC" in blockers:
            eligibility = "MISSING_SPEC"
        elif "GIT_FACTS_UNAVAILABLE" in blockers:
            eligibility = "NEEDS_REVIEW"
        elif "DUPLICATE_REMOTE" in blockers:
            eligibility = "DUPLICATE_REMOTE"
        elif "SHALLOW_CLONE" in blockers:
            eligibility = "NEEDS_HISTORY_FETCH"
        elif blockers:
            eligibility = "NEEDS_REVIEW"
        else:
            eligibility = "CANDIDATE"
        result.append(
            {
                "feature": feature,
                "relative_path": f"{prefix}{feature}",
                "status": "HAS_SPEC" if "spec.md" in top_level_files else "MISSING_SPEC",
                "discovery_status": "HAS_SPEC" if "spec.md" in top_level_files else "MISSING_SPEC",
                "eligibility": eligibility,
                "blockers": sorted(set(blockers)),
                "missing_stage_documents": missing_documents,
                "files": sorted(files),
                "git_commit": head,
            }
        )
    return result


def discover(root: Path, *, max_depth: int = 3) -> dict[str, Any]:
    expanded_root = root.expanduser()
    if expanded_root.is_symlink():
        raise ValueError(f"根目录不能是符号链接：{expanded_root}")
    resolved_root = expanded_root.resolve()
    if not resolved_root.is_dir() or not os.access(resolved_root, os.R_OK | os.X_OK):
        raise ValueError(f"根目录不存在或不可读：{resolved_root}")
    repository_roots, scan_errors, depth_limit_reached = _repository_roots(resolved_root, max_depth)
    repository_facts: list[dict[str, Any]] = []
    canonical_remote_counts: Counter[str] = Counter()
    for repository in repository_roots:
        git_errors: list[str] = []

        def read_fact(
            label: str,
            *arguments: str,
            allowed_returncodes: tuple[int, ...] = (0,),
        ) -> str:
            output, error = _git_command(
                repository,
                *arguments,
                allowed_returncodes=allowed_returncodes,
            )
            if error:
                git_errors.append(f"{label}: {error}")
            return output

        head = read_fact("HEAD", "rev-parse", "HEAD")
        tree_output = read_fact("HEAD tree", "ls-tree", "-r", "HEAD") if head else ""
        head_paths = _head_paths(tree_output)
        remote_names = read_fact("remote list", "remote").splitlines()
        remote = read_fact("origin remote", "remote", "get-url", "origin") if "origin" in remote_names else ""
        shallow_text = read_fact("shallow state", "rev-parse", "--is-shallow-repository")
        if shallow_text not in {"true", "false"}:
            git_errors.append(f"shallow state: 非法结果 {shallow_text!r}")
            shallow: bool | None = None
        else:
            shallow = shallow_text == "true"
        gitmodules = read_fact(".gitmodules", "show", "HEAD:.gitmodules") if ".gitmodules" in head_paths else ""
        lfs_output = read_fact(
            "Git LFS pointers",
            "grep",
            "-I",
            "-l",
            "--fixed-strings",
            _LFS_MARKER,
            "HEAD",
            "--",
            allowed_returncodes=(0, 1),
        ) if head else ""
        canonical_remote = _canonical_remote(remote) if remote else ""
        if canonical_remote:
            canonical_remote_counts[canonical_remote] += 1
        relative_repository = "." if repository == resolved_root else repository.relative_to(resolved_root).as_posix()
        scan_errors.extend(f"{relative_repository}: {error}" for error in git_errors)
        repository_facts.append(
            {
                "repository": repository,
                "head": head,
                "head_paths": head_paths,
                "remote": canonical_remote,
                "shallow": shallow,
                "submodule_paths": sorted(
                    set(_submodule_paths(gitmodules)) | set(_gitlink_paths(tree_output))
                )
                if head
                else [],
                "lfs_pointer_paths": _lfs_pointer_paths(lfs_output) if head else [],
                "git_errors": git_errors,
            }
        )

    repositories: list[dict[str, Any]] = []
    for fact in repository_facts:
        repository = fact["repository"]
        remote = fact["remote"]
        blockers: list[str] = []
        if fact["git_errors"]:
            blockers.append("GIT_FACTS_UNAVAILABLE")
        if fact["shallow"]:
            blockers.append("SHALLOW_CLONE")
        if fact["submodule_paths"]:
            blockers.append("SUBMODULES_PRESENT")
        if fact["lfs_pointer_paths"]:
            blockers.append("GIT_LFS_POINTERS")
        if remote and canonical_remote_counts[remote] > 1:
            blockers.append("DUPLICATE_REMOTE")
        features = _features(
            repository,
            fact["head_paths"],
            head=fact["head"],
            repository_blockers=blockers,
        )
        repositories.append(
            {
                "path": str(repository),
                "relative_path": "." if repository == resolved_root else repository.relative_to(resolved_root).as_posix(),
                "remote": remote or None,
                "remote_fingerprint": hashlib.sha256(remote.encode("utf-8")).hexdigest() if remote else None,
                "head": fact["head"] or None,
                "facts_source": "GIT_HEAD_INCOMPLETE" if fact["git_errors"] else "GIT_HEAD",
                "shallow": fact["shallow"],
                "build_system": _build_system(set(fact["head_paths"])),
                "submodule_paths": fact["submodule_paths"],
                "lfs_pointer_paths": fact["lfs_pointer_paths"],
                "git_errors": fact["git_errors"],
                "blockers": sorted(blockers),
                "status": (
                    "GIT_FACTS_UNAVAILABLE"
                    if fact["git_errors"]
                    else "HAS_COMPLETED_FEATURES" if features else "NO_COMPLETED_FEATURES"
                ),
                "features": features,
            }
        )
    eligibility_counts = Counter(
        feature["eligibility"]
        for repository in repositories
        for feature in repository["features"]
    )
    discovery_counts = Counter(
        feature["discovery_status"]
        for repository in repositories
        for feature in repository["features"]
    )
    return {
        "schema_version": "helixharness-case-creator-discovery.v2",
        "root": str(resolved_root),
        "facts_source": "GIT_HEAD",
        "max_depth": max_depth,
        "depth_limit_reached": depth_limit_reached,
        "scan_errors": scan_errors,
        "repository_count": len(repositories),
        "feature_count": sum(eligibility_counts.values()),
        "summary": {
            "discovery_status": dict(sorted(discovery_counts.items())),
            "eligibility": dict(sorted(eligibility_counts.items())),
        },
        "repositories": repositories,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        payload = discover(arguments.root, max_depth=max(0, arguments.max_depth))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
