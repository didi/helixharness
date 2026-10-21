"""Tests for the HelixHarness Git synchronization helper."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from eval.cli.sync_helixharness_usage import _eval_inventory, _load_state, _sync_repo, _write_state


def test_failed_sync_preserves_only_still_enabled_source_cache(tmp_path, monkeypatch):
    from eval.cli import sync_helixharness_usage as sync

    cache = tmp_path / "cache"
    removed, enabled = cache / "removed", cache / "enabled"
    for root in (removed, enabled):
        (root / "docs/eval").mkdir(parents=True)
    sync._write_roots(cache, [removed, enabled])
    config = tmp_path / "sources.yaml"
    config.write_text(json.dumps({"repos": [{"name": "enabled", "url": (tmp_path / "missing.git").as_uri()}]}))
    monkeypatch.setattr(sys, "argv", ["sync", "--config", str(config), "--cache-dir", str(cache), "--skip-ai"])
    assert sync.main() == 1
    assert sync._read_roots(cache) == [enabled]
    assert removed.is_dir()  # Source history is preserved, but no longer active.
    config.write_text('{"repos": []}')
    assert sync.main() == 0
    assert sync._read_roots(cache) == []
    assert enabled.is_dir()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_full_history_mode_unshallows_existing_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "master")
    _git(source, "config", "user.name", "HelixHarness Test")
    _git(source, "config", "user.email", "helixharness-platform-test@example.com")
    eval_dir = source / "docs" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / ".keep").write_text("first\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "first")
    (eval_dir / ".keep").write_text("second\n", encoding="utf-8")
    _git(source, "commit", "-qam", "second")

    repo = {"name": "demo", "url": source.as_uri(), "branch": "master", "eval_dir": "docs/eval"}
    cache_dir = tmp_path / "cache"
    state: dict = {}
    shallow_result = _sync_repo(repo, {}, cache_dir, 1, state)
    target = shallow_result["root"]
    assert _git(target, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(target, "rev-list", "--count", "HEAD") == "1"

    full_result = _sync_repo(repo, {}, cache_dir, 0, state)
    assert full_result["status"] == "unchanged"
    assert _git(target, "rev-parse", "--is-shallow-repository") == "false"
    assert _git(target, "rev-list", "--count", "HEAD") == "2"


def test_inventory_counts_current_schema_and_rejects_retired_schema(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    _write_state(cache_dir, {"repos": {"demo": {"status": "updated"}}})

    state = _load_state(cache_dir)
    assert state["cache_schema_version"] == "1.0"
    assert state["telemetry_schema_version"] == "1.0"
    assert state["repos"]["demo"]["status"] == "updated"

    eval_root = tmp_path / "repo" / "docs" / "eval"
    legacy_dir = eval_root / "legacy"
    workflow_dir = eval_root / "workflow"
    invalid_dir = eval_root / "invalid"
    legacy_dir.mkdir(parents=True)
    workflow_dir.mkdir(parents=True)
    invalid_dir.mkdir(parents=True)
    (legacy_dir / "eval-log.json").write_text(
        '{"schema_version":"1.0","entries":[]}',
        encoding="utf-8",
    )
    (workflow_dir / "eval-log.json").write_text(
        '{"schema":"helixharness-eval-ledger/1.0","schema_version":"1.0","entries":[]}',
        encoding="utf-8",
    )
    (invalid_dir / "eval-log.json").write_text(
        '{"schema":"unknown-ledger/9.0","schema_version":"9.0","entries":[]}',
        encoding="utf-8",
    )

    inventory = _eval_inventory(tmp_path / "repo", "docs/eval")
    assert inventory == {
        "eval_log_count": 3,
        "schema_2_log_count": 1,
        "valid_log_count": 1,
        "invalid_log_count": 2,
    }


def test_sync_cli_summary_separates_repositories_with_valid_eval_from_eval_record_count(tmp_path: Path) -> None:
    from eval.tests.test_usage_loader import _workflow_log

    sources = []
    for repo_name, logs in (
        (
            "repo-a",
            [
                _workflow_log(),
                _workflow_log(),
                {"schema": "unknown-ledger/9.0", "schema_version": "9.0", "entries": []},
            ],
        ),
        (
            "repo-b",
            [_workflow_log()],
        ),
    ):
        source = tmp_path / repo_name
        source.mkdir()
        _git(source, "init", "-q", "-b", "master")
        _git(source, "config", "user.name", "HelixHarness Test")
        _git(source, "config", "user.email", "helixharness-platform-test@example.com")
        for index, log in enumerate(logs):
            feature_dir = source / "docs" / "eval" / f"feature-{index}"
            feature_dir.mkdir(parents=True)
            (feature_dir / "eval-log.json").write_text(
                json.dumps(log),
                encoding="utf-8",
            )
        _git(source, "add", ".")
        _git(source, "commit", "-qm", "add eval logs")
        sources.append((repo_name, source))

    config_path = tmp_path / "usage_repos.yaml"
    config_path.write_text(
        "repos:\n"
        + "".join(
            f"  - name: {name}\n    url: {json.dumps(source.as_uri())}\n    branch: master\n"
            for name, source in sources
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    script = Path(__file__).parents[1] / "cli" / "sync_helixharness_usage.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(config_path),
            "--cache-dir",
            str(cache_dir),
            "--skip-ai",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    last_run = json.loads((cache_dir / "sync_state.json").read_text(encoding="utf-8"))["last_run"]
    assert last_run["with_eval"] == 2
    assert last_run["valid_eval_log_count"] == 3
    assert last_run["invalid_eval_log_count"] == 1
    assert Path(__import__("os").environ["HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH"]).is_file()
    assert "retention cleanup checked" in result.stdout

    from eval.usage_first_seen import UsageFirstSeenStore

    admissions = UsageFirstSeenStore(cache_dir).snapshot()
    assert len(admissions["projects"]) == 3
    assert all(admissions["projects"].values())
    # A second real local-Git sync must retain the same admission history.
    subprocess.run(result.args, check=True, capture_output=True, text=True)
    assert UsageFirstSeenStore(cache_dir).snapshot() == admissions
