"""Read-only, feature-level code metrics from immutable archive checkpoints.

These metrics are dashboard projections, not stage snapshots or scoring evidence.
Missing commits or scope remain unavailable; no branch or worktree fallback is used.
"""
from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from eval.stage_supplements import LEDGER_SCHEMA, LEDGER_SCHEMA_VERSION

ARCHIVE_REASONS = {
    "archive_not_completed": "需求尚未完成归档",
    "archive_checkpoint_missing": "缺少已完成的归档记录",
    "archive_commit_missing": "缺少明确的归档基线或实现提交",
    "archive_scope_missing": "归档记录未提供明确的文件范围",
    "archive_scope_invalid": "归档文件范围无效",
    "archive_git_unavailable": "本地提交不可用或 Git 统计失败，需检查仓库同步",
    "archive_repository_missing": "缺少本地仓库",
    "archive_documents_missing": "尚未找到与本次归档记录对应的文档提交，需等待仓库同步或补充归档证据",
    "archive_documents_ambiguous": "存在多个匹配的归档文档提交，无法确定本次归档产物",
    "archive_documents_search_limit": "归档文档历史超出检索上限，需补充明确的归档提交",
}


def _git(root: str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-pager", "--literal-pathspecs", "-C", root, *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    ).stdout


def _category(path: str) -> str:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    suffix = PurePosixPath(name).suffix
    if "docs" in parts or suffix in {".md", ".rst", ".adoc"} or name.startswith(("readme", "changelog", "license")):
        return "docs"
    if any(p in {"test", "tests", "__tests__", "spec", "specs"} for p in parts if p != name) and "docs" not in parts:
        return "test"
    if name.startswith("test_") or re.search(r"(?:[._](?:test|spec)|_test)\.[^.]+$", name):
        return "test"
    if "src" in parts or suffix in {".py", ".java", ".kt", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx", ".vue", ".rb", ".php", ".c", ".h", ".cpp", ".cs", ".swift", ".sql"}:
        return "prod"
    return "other"



def _knowledge_title(root: str, revision: str, path: str) -> str:
    """Read only a document heading from the fixed Git revision, never its body or a generated summary."""
    try:
        heading = _git(root, "grep", "--no-textconv", "-h", "-e", "^# ", revision, "--", path)
        lines = heading.decode("utf-8", errors="replace").splitlines()
        return lines[0].removeprefix("# ").strip()[:200] if lines else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=256)
def _committed_counts(root: str, base: str, head: str, paths: tuple[str, ...]) -> dict[str, Any]:
    """Cache only successful diffs keyed by resolved immutable commit IDs."""
    raw = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base, head, "--", *paths)
    status_raw = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", base, head, "--", *paths)
    status_parts = status_raw.split(b"\0")[:-1]
    if len(status_parts) % 2:
        raise ValueError("invalid name-status")
    statuses = dict(zip(status_parts[1::2], status_parts[::2]))
    changes = []
    counts = {"files_changed": 0, "lines_added": 0, "lines_deleted": 0, "binary_files_changed": 0}
    for category in ("prod", "test", "docs", "other"):
        counts.update({f"{category}_{key}": 0 for key in ("files_changed", "lines_added", "lines_deleted")})
    for row in raw.split(b"\0"):
        if not row:
            continue
        added, deleted, path = row.split(b"\t", 2)
        status = statuses.get(path)
        if status not in {b"A", b"M", b"D", b"T"}:
            raise ValueError("unsupported file status")
        binary = added == deleted == b"-"
        changes.append({
            "path": path.decode("utf-8", errors="replace"), "status": status.decode("ascii"),
            "lines_added": None if binary else int(added),
            "lines_deleted": None if binary else int(deleted), "binary": binary,
        })
        if changes[-1]["path"].startswith("docs/knowledge/") and changes[-1]["path"].endswith(".md"):
            changes[-1]["title"] = _knowledge_title(root, base if status == b"D" else head, changes[-1]["path"])
        category = _category(path.decode("utf-8", errors="replace"))
        counts["files_changed"] += 1
        counts[f"{category}_files_changed"] += 1
        if added == deleted == b"-":
            counts["binary_files_changed"] += 1
            continue
        for key, value in (("lines_added", int(added)), ("lines_deleted", int(deleted))):
            if value < 0:
                raise ValueError("invalid numstat")
            counts[key] += value
            counts[f"{category}_{key}"] += value
    return {**counts, "file_changes": changes}


def _valid_paths(paths: Any) -> bool:
    return isinstance(paths, list) and 0 < len(paths) <= 10000 and all(
        isinstance(p, str) and p and not p.startswith(("/", ":")) and "\\" not in p and "\0" not in p
        and all(part not in {"", ".", ".."} for part in p.split("/"))
        for p in paths
    )


def _archive_document_candidate(
    root: str, commit: str, log: dict, archive: dict, implementation: str,
    completed: list[str], allowed: set[str], ledger_path: str,
) -> tuple[str, list[str]] | None:
    """Reuse immutable receipt checks while discovering candidate commits live."""
    identity = {k: log.get(k) for k in ("schema", "schema_version", "feature", "requirement", "status")}
    archive_identity = {k: archive.get(k) for k in (
        "entry_id", "command", "stage_kind", "status", "started_at", "ended_at", "checkpoint",
    )}
    return _fixed_archive_document_candidate(
        root, commit, json.dumps(identity, sort_keys=True), json.dumps(archive_identity, sort_keys=True),
        implementation, tuple(completed), tuple(sorted(allowed)), ledger_path,
    )


@lru_cache(maxsize=256)
def _fixed_archive_document_candidate(
    root: str, commit: str, identity_json: str, archive_json: str, implementation: str,
    completed_paths: tuple[str, ...], allowed_paths: tuple[str, ...], ledger_path: str,
) -> tuple[str, list[str]] | None:
    """Cache fixed SHA metadata only; failed Git reads are never cached."""
    log, archive = json.loads(identity_json), json.loads(archive_json)
    completed, allowed = list(completed_paths), set(allowed_paths)
    parents = _git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    if len(parents) != 1:
        return None
    parent = parents[0]
    _git(root, "merge-base", "--is-ancestor", implementation, parent)
    # Read only the ledger, never source or conversation bodies. Bound its size.
    if int(_git(root, "cat-file", "-s", f"{commit}:{ledger_path}")) > 8 * 1024 * 1024:
        return None
    receipt = json.loads(_git(root, "show", f"{commit}:{ledger_path}"))
    if not isinstance(receipt, dict) or any(receipt.get(k) != log.get(k) for k in ("schema", "schema_version", "feature", "requirement", "status")):
        return None
    entries = [e for e in receipt.get("entries", []) if isinstance(e, dict) and (e.get("command") == "archive" or e.get("stage_kind") == "archive")]
    if not entries or any(entries[-1].get(k) != archive.get(k) for k in (
        "entry_id", "command", "stage_kind", "status", "started_at", "ended_at", "checkpoint",
    )):
        return None
    artifacts = entries[-1].get("artifacts")
    if isinstance(artifacts, dict):
        artifacts = artifacts.get("produced_files")
    if artifacts != completed:
        return None
    raw = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", parent, commit)
    parts = raw.split(b"\0")[:-1]
    statuses = {p.decode("utf-8"): s.decode("ascii") for s, p in zip(parts[::2], parts[1::2])}
    if not set(statuses) <= allowed:
        return None
    # Each recorded completed artifact must be introduced here with its active
    # predecessor removed. Merely editing the same directory later is not proof.
    if any(statuses.get(p) != "A" or statuses.get(p.replace("docs/specs/completed/", "docs/specs/active/", 1)) != "D" for p in completed):
        return None
    return parent, sorted(p for p in statuses if p.startswith("docs/") and p != ledger_path)


@lru_cache(maxsize=1024)
def _fixed_blob_id(root: str, commit: str, path: str) -> bytes:
    """Blob identity is immutable once the full commit SHA is resolved."""
    return _git(root, "rev-parse", f"{commit}:{path}")


def _archive_documents(log: dict, archive: dict, root: str, implementation: str, paths: list[str]) -> dict:
    """Find unique immutable archive evidence; branches are discovery inputs only."""
    missing = {"collection_status": "unavailable", "unavailable_reason": "archive_documents_missing"}
    feature = log.get("feature")
    if not isinstance(feature, str) or not _valid_paths([feature]) or "/" in feature or not archive.get("entry_id"):
        return missing
    completed = archive.get("artifacts")
    if isinstance(completed, dict):
        completed = completed.get("produced_files")
    prefix = f"docs/specs/completed/{feature}/"
    if not _valid_paths(completed) or not all(p.startswith(prefix) for p in completed):
        return missing
    ledger_path = f"docs/eval/{feature}/eval-log.json"
    active = [p.replace("docs/specs/completed/", "docs/specs/active/", 1) for p in completed]
    allowed = set(paths) | set(completed) | set(active) | {ledger_path}
    try:
        commits = _git(root, "log", "--all", "HEAD", "--format=%H", "--max-count=65", "--diff-filter=A", "--no-renames", "--", *completed).decode().splitlines()
        if len(commits) >= 65:
            return {**missing, "unavailable_reason": "archive_documents_search_limit"}
        matches = []
        for commit in commits:
            try:
                match = _archive_document_candidate(root, commit, log, archive, implementation, completed, allowed, ledger_path)
            except (subprocess.SubprocessError, ValueError, UnicodeError):
                continue
            if match:
                matches.append((commit, *match))
        if len(matches) > 1:
            return {**missing, "unavailable_reason": "archive_documents_ambiguous"}
        if not matches:
            return missing
        commit, parent, doc_paths = matches[0]
        changes = deepcopy(_committed_counts(root, parent, commit, tuple(doc_paths))["file_changes"])
        # Pair explicit active/completed identities instead of similarity guesses.
        rows = {item["path"]: item for item in changes}
        for before, after in zip(active, completed):
            same = _fixed_blob_id(root, parent, before) == _fixed_blob_id(root, commit, after)
            rows.pop(before)
            rows[after].update(status="R" if same else "RM", previous_path=before)
            # Destination additions are not the edit size of a moved document.
            rows[after].pop("lines_added", None)
            rows[after].pop("lines_deleted", None)
        return {
            "collection_status": "collected", "unavailable_reason": None,
            "base_ref": parent, "head_ref": commit, "source": "archive_document_commit",
            "file_changes": list(rows.values()),
        }
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return missing


def archive_code_delta(log: dict[str, Any], root: str) -> dict[str, Any]:
    """Resolve current ledgers independently of host and plugin release version."""
    def unavailable(reason: str) -> dict[str, Any]:
        return {"collection_status": "unavailable", "unavailable_reason": reason}

    if log.get("schema") != LEDGER_SCHEMA or log.get("schema_version") != LEDGER_SCHEMA_VERSION:
        return unavailable("unsupported_archive_schema")
    if log.get("status") != "archived":
        return unavailable("archive_not_completed")
    archives = [e for e in log.get("entries", []) if isinstance(e, dict) and (e.get("stage_kind") == "archive" or e.get("command") == "archive")]
    if not archives or archives[-1].get("status") != "completed":
        return unavailable("archive_checkpoint_missing")
    checkpoint = archives[-1].get("checkpoint")
    if not isinstance(checkpoint, dict):
        return unavailable("archive_commit_missing")
    # The Archive start may already include the implementation. Its frozen
    # review baseline identifies the full requirement, including earlier commits.
    base = checkpoint.get("review_fixed_point", checkpoint.get("base_commit"))
    head = checkpoint.get("implementation_commit_id")
    review_range = not head and checkpoint.get("code_resolution") == "branch"
    if review_range:
        # base_commit is the Archive command's start, often equal to head_commit.
        # The frozen review pair describes the known implementation range.
        base, head = checkpoint.get("review_fixed_point"), checkpoint.get("review_head")
    if not all(isinstance(ref, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) for ref in (base, head)):
        return unavailable("archive_commit_missing")
    paths = checkpoint.get("reviewed_paths")
    if paths is None or paths == []:
        return unavailable("archive_scope_missing")
    if not _valid_paths(paths):
        return unavailable("archive_scope_invalid")
    if not root or not Path(root).is_dir():
        return unavailable("archive_repository_missing")
    try:
        resolved_root = str(Path(root).resolve())
        base_sha, head_sha = [
            _git(resolved_root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii").strip()
            for ref in (base, head)
        ]
        if review_range:
            observed_head = checkpoint.get("head_commit")
            if not isinstance(observed_head, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", observed_head):
                return unavailable("archive_commit_missing")
            if _git(resolved_root, "rev-parse", "--verify", f"{observed_head}^{{commit}}").decode().strip() != head_sha:
                return unavailable("archive_commit_missing")
            _git(resolved_root, "merge-base", "--is-ancestor", base_sha, head_sha)
        counts = _committed_counts(resolved_root, base_sha, head_sha, tuple(sorted(set(paths))))
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return unavailable("archive_git_unavailable")
    result = {
        **deepcopy(counts), "scope": "archive_review" if review_range else "feature_archive", "source": "archive_git_numstat",
        "base_ref": base_sha, "head_ref": head_sha, "path_filter": "reviewed_paths",
        "attribution": "archive_review_scope", "confidence": "medium", "contamination_risk": "unknown",
        "collection_status": "collected", "unavailable_reason": None,
    }
    if review_range:
        result["archive_documents"] = _archive_documents(log, archives[-1], resolved_root, head_sha, paths)
    return result
