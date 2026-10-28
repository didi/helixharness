"""Archive metrics use local immutable Git objects, not the current worktree."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

import eval.code_metrics as code_metrics
from eval.code_metrics import archive_code_delta
from eval.usage_loader import aggregate_all, aggregate_feature, discover_projects, render_usage_overview_html


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def archived_repo(tmp_path: Path) -> tuple[Path, dict]:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.hooksPath", "/dev/null")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Fixture")
    files = {
        "src/app.py": "old\n",
        "src/deleted.py": "removed\n",
        "docs/readme.md": "before\n",
        "src/a[1].py": "old\n",
        "src/a1.py": "old\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src/app.py").write_text("new\nextra\n")
    (tmp_path / "src/deleted.py").unlink()
    (tmp_path / "docs/readme.md").write_text("before\nafter\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("test one\ntest two\n")
    (tmp_path / "src/asset.bin").write_bytes(b"\0binary")
    (tmp_path / "src/a[1].py").write_text("old\nliteral\n")
    (tmp_path / "src/a1.py").write_text("unrelated\n" * 30)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "implementation")
    head = git(tmp_path, "rev-parse", "HEAD")
    paths = ["src/app.py", "src/deleted.py", "docs/readme.md", "tests/test_app.py", "src/asset.bin", "src/a[1].py"]
    entry = {
        "entry_id": "entry-archive", "command": "archive", "stage_kind": "archive",
        "helixharness_version": "2.2.9", "attempt": 1,
        "trigger": "user_command", "platform": "codex", "session_hash": "session-fixture",
        "started_at": "2026-09-08T08:00:00Z", "ended_at": "2026-09-08T08:01:00Z",
        "status": "completed", "actor": {"user_name": "Fixture"}, "artifacts": [], "quality": {},
        "checkpoint": {"base_commit": base, "implementation_commit_id": head, "reviewed_paths": paths},
    }
    log = {
        "schema": "helixharness-eval-ledger/1.0", "schema_version": "1.0", "status": "archived",
        "feature": "archive-metrics", "project": {"name": "fixture"},
        "requirement": {"id": "req-fixture", "name": "archive-metrics"},
        "created_at": entry["started_at"], "last_updated": entry["ended_at"], "entries": [entry],
    }
    return tmp_path, log


def test_archive_counts_fixed_scope_and_keeps_binary_separate(archived_repo):
    root, log = archived_repo
    (root / "src/app.py").write_text("later work\n" * 50)
    git(root, "add", ".")
    git(root, "commit", "-qm", "later unrelated work")
    (root / "src/app.py").write_text("dirty work\n" * 100)
    original = copy.deepcopy(log)

    result = archive_code_delta(log, str(root))

    assert result["collection_status"] == "collected"
    assert (result["files_changed"], result["lines_added"], result["lines_deleted"]) == (6, 6, 2)
    assert (result["prod_files_changed"], result["prod_lines_added"], result["prod_lines_deleted"]) == (4, 3, 2)
    assert result["test_lines_added"] == 2
    assert result["docs_lines_added"] == 1
    assert result["binary_files_changed"] == 1
    changes = {item["path"]: item for item in result["file_changes"]}
    assert {path: item["status"] for path, item in changes.items()} == {
        "src/app.py": "M", "src/deleted.py": "D", "docs/readme.md": "M",
        "tests/test_app.py": "A", "src/asset.bin": "A", "src/a[1].py": "M",
    }
    assert changes["src/app.py"]["lines_added"] == 2
    assert changes["src/deleted.py"]["lines_deleted"] == 1
    assert changes["src/asset.bin"]["binary"] is True
    assert changes["src/asset.bin"]["lines_added"] is None
    assert log == original
    assert (root / "src/app.py").read_text() == "dirty work\n" * 100


def test_committed_implementation_uses_review_baseline_before_archive_start(archived_repo):
    root, log = archived_repo
    checkpoint = log["entries"][-1]["checkpoint"]
    checkpoint["review_fixed_point"] = checkpoint["base_commit"]
    checkpoint["base_commit"] = checkpoint["implementation_commit_id"]
    checkpoint["review_head"] = checkpoint["implementation_commit_id"]
    checkpoint["code_resolution"] = "commit"
    result = archive_code_delta(log, str(root))
    assert (result["lines_added"], result["lines_deleted"]) == (6, 2)


def test_invalid_frozen_review_baseline_does_not_fall_back_to_archive_start(archived_repo):
    root, log = archived_repo
    log["entries"][-1]["checkpoint"]["review_fixed_point"] = "HEAD"
    assert archive_code_delta(log, str(root))["unavailable_reason"] == "archive_commit_missing"


@pytest.mark.parametrize("version,platform", [
    ("1.0.0", "claude-code"),
    ("1.0.0", "codex"),
    ("99.0.0", "claude-code"),
])
def test_supported_schema_is_independent_of_plugin_and_host(archived_repo, version, platform):
    root, log = archived_repo
    entry = log["entries"][-1]
    entry.update(helixharness_version=version, platform=platform)
    cp = entry["checkpoint"]
    cp["code_resolution"] = "commit"
    assert archive_code_delta(log, str(root))["lines_added"] == 6


@pytest.mark.parametrize("head_key", ["head_commit", "checkpoint_commit"])
def test_retired_commit_fields_do_not_replace_frozen_implementation(archived_repo, head_key):
    root, log = archived_repo
    cp = log["entries"][-1]["checkpoint"]
    cp[head_key] = cp.pop("implementation_commit_id")
    cp["code_resolution"] = "commit"
    assert archive_code_delta(log, str(root)) == {
        "collection_status": "unavailable", "unavailable_reason": "archive_commit_missing",
    }


@pytest.mark.parametrize("change,reason", [
    ({"reviewed_paths": None}, "archive_scope_missing"),
    ({"reviewed_paths": []}, "archive_scope_missing"),
    ({"reviewed_paths": ["../outside.py"]}, "archive_scope_invalid"),
    ({"reviewed_paths": [":(glob)**"]}, "archive_scope_invalid"),
    ({"reviewed_paths": "src/app.py"}, "archive_scope_invalid"),
    ({"reviewed_paths": ["/src/app.py"]}, "archive_scope_invalid"),
    ({"base_commit": "HEAD"}, "archive_commit_missing"),
    ({"implementation_commit_id": "HEAD"}, "archive_commit_missing"),
    ({"implementation_commit_id": "f" * 40}, "archive_git_unavailable"),
])
def test_missing_or_unsafe_evidence_never_becomes_zero(archived_repo, change, reason):
    root, log = archived_repo
    log["entries"][-1]["checkpoint"].update(change)
    result = archive_code_delta(log, str(root))
    assert result == {"collection_status": "unavailable", "unavailable_reason": reason}
    assert "lines_added" not in result


def test_requires_completed_archive_and_does_not_fall_back_to_branch(archived_repo):
    root, log = archived_repo
    log["status"] = "in_progress"
    assert archive_code_delta(log, str(root))["unavailable_reason"] == "archive_not_completed"
    log["status"] = "archived"
    log["entries"][-1]["status"] = "failed"
    assert archive_code_delta(log, str(root))["unavailable_reason"] == "archive_checkpoint_missing"
    log["entries"][-1]["status"] = "completed"
    cp = log["entries"][-1]["checkpoint"]
    cp["head_commit"] = cp.pop("implementation_commit_id")
    cp["code_resolution"] = "branch"
    assert archive_code_delta(log, str(root))["unavailable_reason"] == "archive_commit_missing"


def test_successful_zero_diff_is_distinct_from_missing_evidence(archived_repo):
    root, log = archived_repo
    cp = log["entries"][-1]["checkpoint"]
    cp["implementation_commit_id"] = cp["base_commit"]
    result = archive_code_delta(log, str(root))
    assert result["collection_status"] == "collected"
    assert result["files_changed"] == result["lines_added"] == result["lines_deleted"] == 0


def test_discovery_aggregation_and_html_count_archive_once_without_supplements(archived_repo):
    root, log = archived_repo
    archive = log["entries"][-1]
    for i in range(3):
        apply = {**archive, "entry_id": f"entry-apply-{i}", "command": "apply", "stage_kind": "coding"}
        log["entries"].insert(0, apply)
    # A repeated archive receipt must not double the feature-level net change.
    log["entries"].append({**archive, "entry_id": "entry-archive-repeat"})
    path = root / "docs/eval/archive-metrics/eval-log.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps(log)
    path.write_text(raw)
    result = aggregate_all(discover_projects([str(root)], excluded_features=set()))
    delta = result["features"][0]["code_delta"]
    assert delta["lines_added"] == result["summary"]["code_delta"]["lines_added"] == 6
    assert delta["scope"] == "feature_archive"
    assert all("worktree_delta" not in e for e in result["features"][0]["entries"])
    rendered = render_usage_overview_html([str(root)])
    assert "归档净代码变更" in rendered
    assert "+6 / -2" in rendered
    assert "更新内容 · 知识库更新 0 篇" in rendered
    assert "可解析的归档范围内未发现知识文档变更" in rendered
    assert "src/app.py</code>" in rendered and "src/deleted.py</code>" in rendered
    assert "截至归档 · 5 个文件 · 新增 2 · 修改 2 · 删除 1" in rendered
    assert rendered.count('class="helixharness-implementation-files"') == 1
    assert 'class="helixharness-archive-file-changes"' not in rendered
    assert '<details class="helixharness-archive-knowledge" open' not in rendered
    before_archive = rendered.split('归档</b>', 1)[0]
    assert 'helixharness-archive-knowledge"><summary>' not in before_archive
    assert path.read_text() == raw


def test_missing_scope_is_explained_in_detail(archived_repo):
    root, log = archived_repo
    del log["entries"][-1]["checkpoint"]["reviewed_paths"]
    path = root / "docs/eval/archive-metrics/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(log))
    rendered = render_usage_overview_html([str(root)])
    assert "归档记录未提供明确的文件范围" in rendered
    assert "待解析" not in rendered


def test_existing_v2_worktree_statistics_are_not_added_to_archive_diff(archived_repo):
    root, log = archived_repo
    log["entries"][0]["worktree_delta"] = {
        "collection_status": "collected", "files_changed": 1,
        "lines_added": 12, "lines_deleted": 4, "source": "worktree_snapshot",
    }
    result = aggregate_feature(log, str(root))["code_delta"]
    assert (result["files_changed"], result["lines_added"], result["lines_deleted"]) == (1, 12, 4)
    assert result["source"] == "worktree_snapshot"
    changes = aggregate_feature(log, str(root))["archive_changes"]["file_changes"]
    assert len(changes) == 6


def test_temporary_missing_objects_can_be_resolved_on_next_read(archived_repo, monkeypatch):
    root, log = archived_repo
    original = code_metrics._git

    def missing(*args):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(code_metrics, "_git", missing)
    assert archive_code_delta(log, str(root))["unavailable_reason"] == "archive_git_unavailable"
    monkeypatch.setattr(code_metrics, "_git", original)
    assert archive_code_delta(log, str(root))["lines_added"] == 6


def test_numstat_handles_renames_and_unusual_literal_paths(archived_repo):
    root, log = archived_repo
    checkpoint = log["entries"][-1]["checkpoint"]
    checkpoint["base_commit"] = git(root, "rev-parse", "HEAD")
    old = "src/app.py"
    new = "src/renamed\twith\nnewline.py"
    (root / old).rename(root / new)
    git(root, "add", ".")
    git(root, "commit", "-qm", "rename")
    checkpoint["implementation_commit_id"] = git(root, "rev-parse", "HEAD")
    checkpoint["reviewed_paths"] = [old, new, new]
    result = archive_code_delta(log, str(root))
    assert (result["files_changed"], result["lines_added"], result["lines_deleted"]) == (2, 2, 2)
    assert {item["path"]: item["status"] for item in result["file_changes"]} == {old: "D", new: "A"}


def test_unknown_schema_is_not_guessed_as_a_known_version(archived_repo):
    root, log = archived_repo
    log.update(schema="helixharness-eval-ledger/3.0", schema_version="3.0")
    path = root / "docs/eval/archive-metrics/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(log))
    projects = discover_projects([str(root)], excluded_features=set())
    assert projects[0]["features"] == []
    assert len(projects[0]["invalid_logs"]) == 1


def test_file_change_projection_is_escaped_and_cached_values_are_not_mutable(archived_repo):
    root, log = archived_repo
    name = "docs/knowledge/<sample>&.md"
    (root / name).parent.mkdir(parents=True)
    (root / name).write_text("# <标题>&\n正文不展示\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "unusual filename")
    checkpoint = log["entries"][-1]["checkpoint"]
    checkpoint["implementation_commit_id"] = git(root, "rev-parse", "HEAD")
    checkpoint["reviewed_paths"].append(name)
    result = archive_code_delta(log, str(root))
    result["file_changes"].clear()
    assert len(archive_code_delta(log, str(root))["file_changes"]) == 7
    path = root / "docs/eval/archive-metrics/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(log))
    html = render_usage_overview_html([str(root)])
    assert "docs/knowledge/&lt;sample&gt;&amp;.md</code>" in html
    assert "docs/knowledge/<sample>&.md" not in html
    assert "&lt;标题&gt;&amp;</b>" in html
    assert "正文不展示" not in html


def test_archive_artifacts_show_knowledge_documents_and_indexes_not_workflow_moves(archived_repo):
    root, log = archived_repo
    for name, title in [("docs/knowledge/api/service.md", "服务接口"), ("docs/knowledge/index.md", "知识索引"), ("docs/knowledge/old.md", "旧知识")]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {title}\n原有内容\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "knowledge baseline")
    cp = log["entries"][-1]["checkpoint"]
    cp["base_commit"] = git(root, "rev-parse", "HEAD")
    changes = {
        "docs/knowledge/api/service.md": "# 服务接口\n不应展示的正文标记\n",
        "docs/knowledge/index.md": "# 知识索引\n新增条目\n",
        "docs/knowledge/new.md": "# 新知识\n不应展示的新正文标记\n",
        "docs/knowledge/session-knowledge-manifest.json": "{}",
        "src/app.py": "unrelated implementation\n",
        "docs/specs/completed/demo/spec.md": "moved receipt\n",
    }
    for name, content in changes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "docs/knowledge/old.md").unlink()
    git(root, "add", ".")
    git(root, "commit", "-qm", "archive knowledge")
    cp["implementation_commit_id"] = git(root, "rev-parse", "HEAD")
    cp["reviewed_paths"] = [*changes, "docs/knowledge/old.md"]
    log["entries"][-1]["artifacts"] = {"produced_files": ["docs/specs/completed/demo/spec.md"]}
    path = root / "docs/eval/archive-metrics/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(log))
    # Later uncommitted titles must not change this archive's knowledge receipt.
    (root / "docs/knowledge/new.md").write_text("# 后续修改\n")
    html = render_usage_overview_html([str(root)])
    assert "更新内容 · 知识库更新 4 篇" in html
    assert "新增 1 · 更新 2 · 删除 1" in html
    assert "1 项知识库辅助记录变更" in html
    for title in ["服务接口", "知识索引", "新知识", "旧知识"]:
        assert f"{title}</b>" in html
    assert "后续修改" not in html
    assert "docs/specs/completed/demo/spec.md</code>" not in html
    assert "src/app.py</code>" not in html
    assert "不应展示的正文标记" not in html and "不应展示的新正文标记" not in html
