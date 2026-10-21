"""Public checkout layout must survive version freezing, not just UI readiness."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench.helixharness_source import HelixHarnessSourceError, frozen_plugin_tree
from bench.scenario_execution_support import export_git_tree
from bench.scenario_strategy import KnownHelixHarnessStrategyResolver
from bench.task_configuration import task_platform_configuration
from bench.tests.test_task_configuration import _provider


def _git(root: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(root), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("layout", ("", "helixharness", "products/tooling/custom-plugin"))
def test_frozen_plugin_documents_and_export_ignore_current_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Source Test")
    _git(repository, "config", "user.email", "source@example.test")
    plugin = repository / layout
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text('{"name":"helix"}\n')
    (plugin / "commands").mkdir()
    for name in ("propose", "apply", "archive"):
        (plugin / "commands" / f"{name}.md").write_text(f"{name} --loop\n")
    if layout:
        (repository / "helixharness-platform").mkdir()
        (repository / "helixharness-platform/private-to-plugin.txt").write_text("not a plugin asset")
        (repository / "commands").mkdir()
        (repository / "commands/propose.md").write_text("wrong root document")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    base = _git(repository, "rev-parse", "HEAD")
    (plugin / "commands/propose.md").write_text("candidate --loop\n")
    _git(repository, "commit", "-am", "candidate")
    candidate = _git(repository, "rev-parse", "HEAD")
    (plugin / "commands/propose.md").write_text("uncommitted and must not be evaluated\n")

    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", str(plugin))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(tmp_path / "absent.json"))
    assert task_platform_configuration(_provider()).repository_ready

    draft = KnownHelixHarnessStrategyResolver(plugin).draft(
        base_ref=base, candidate_ref=candidate,
        base_workflow_commands=("/helix:propose sample --loop",),
        candidate_workflow_commands=("/helix:propose sample --loop",),
    )
    from bench.scenario_runtime import GitScenarioStrategyValidator

    GitScenarioStrategyValidator(repository, reference_ref=base).validate(draft.evaluation_contract)
    assert draft.strategy_summary["BASE"]["commit"] == base
    assert draft.strategy_summary["CANDIDATE"]["commit"] == candidate
    assert draft.evaluation_contract["strategies"]["BASE"]["adapter"]["source_plugin_path"] == (layout or ".")
    for commit, expected in ((base, "propose --loop\n"), (candidate, "candidate --loop\n")):
        destination = tmp_path / commit
        export_git_tree(repository, frozen_plugin_tree(repository, commit), destination)
        assert (destination / "commands/propose.md").read_text() == expected
        assert not (destination / "helixharness-platform").exists()
        assert not (destination / "helixharness").exists()


def test_frozen_layout_is_not_inferred_from_current_checkout(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Source Test")
    _git(tmp_path, "config", "user.email", "source@example.test")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"name":"helix"}\n')
    (tmp_path / "commands").mkdir()
    for name in ("propose", "apply", "archive"):
        (tmp_path / "commands" / f"{name}.md").write_text(f"{name}\n")
    (tmp_path / "workflow.txt").write_text("standalone baseline")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "standalone")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "helixharness").mkdir()
    _git(tmp_path, "mv", ".claude-plugin", "commands", "workflow.txt", "helixharness/")
    _git(tmp_path, "commit", "-m", "combined repository")
    candidate = _git(tmp_path, "rev-parse", "HEAD")
    assert frozen_plugin_tree(tmp_path, base) == base
    assert frozen_plugin_tree(tmp_path, candidate) != candidate
    draft = KnownHelixHarnessStrategyResolver(tmp_path / "helixharness").draft(
        base_ref=base, candidate_ref=candidate,
        base_workflow_commands=("/helix:archive sample",),
        candidate_workflow_commands=("/helix:archive sample",),
    )
    paths = {
        side: strategy["adapter"]["source_plugin_path"]
        for side, strategy in draft.evaluation_contract["strategies"].items()
    }
    assert paths == {"BASE": ".", "CANDIDATE": "helixharness"}


def test_missing_frozen_manifest_is_not_inferred_from_checkout(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Source Test")
    _git(tmp_path, "config", "user.email", "source@example.test")
    (tmp_path / "source.txt").write_text("synthetic repository")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "without plugin")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"name":"helix"}')
    with pytest.raises(HelixHarnessSourceError, match="missing.*manifest"):
        frozen_plugin_tree(tmp_path, commit)


def _plugin(root: Path, name: str = "helix") -> None:
    import json

    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}))
    (root / "commands").mkdir()
    for command in ("propose", "apply", "archive"):
        (root / "commands" / f"{command}.md").write_text(command + "\n")


def test_ambiguous_plugins_require_explicit_path_and_freeze_it(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    for directory in ("one", "tools/two"):
        _plugin(tmp_path / directory)
    _plugin(tmp_path / "other-product", name="another-plugin")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "two plugins")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(HelixHarnessSourceError, match="Multiple.*one.*tools/two"):
        frozen_plugin_tree(tmp_path, commit)

    draft = KnownHelixHarnessStrategyResolver(tmp_path, plugin_path="tools/two").draft(
        base_ref=commit, candidate_ref=commit,
        base_workflow_commands=("/helix:apply sample", "/helix:archive sample"),
        candidate_workflow_commands=("/helix:apply sample", "/helix:archive sample"),
    )
    adapter = draft.evaluation_contract["strategies"]["BASE"]["adapter"]
    assert adapter["source_plugin_path"] == "tools/two"
    from bench.scenario_runtime import GitScenarioStrategyValidator, _validate_contract

    _validate_contract(draft.evaluation_contract)
    # A later operator choice must not redirect a task already frozen to tools/two.
    GitScenarioStrategyValidator(tmp_path, reference_ref=commit, plugin_path="one").validate(
        draft.evaluation_contract
    )
    assert frozen_plugin_tree(tmp_path, commit, plugin_path=adapter["source_plugin_path"]) == _git(
        tmp_path, "rev-parse", f"{commit}:tools/two"
    )


@pytest.mark.parametrize("path", ("../one", "/one", "one/../one", "one//nested", "one\\nested", "missing", ""))
def test_invalid_explicit_path_never_falls_back(tmp_path: Path, path: str) -> None:
    _git(tmp_path, "init")
    _plugin(tmp_path / "one")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "plugin")
    with pytest.raises(HelixHarnessSourceError):
        frozen_plugin_tree(tmp_path, "HEAD", plugin_path=path)


@pytest.mark.parametrize("invalid", ("wrong-name", "invalid-json", "symlink", "missing-command"))
def test_non_plugin_or_incomplete_manifest_is_not_selected(tmp_path: Path, invalid: str) -> None:
    _git(tmp_path, "init")
    _plugin(tmp_path / "valid")
    _plugin(tmp_path / "decoy")
    manifest = tmp_path / "decoy/.claude-plugin/plugin.json"
    if invalid == "wrong-name":
        manifest.write_text('{"name":"another-plugin"}')
    elif invalid == "invalid-json":
        manifest.write_text("{")
    elif invalid == "symlink":
        manifest.unlink()
        manifest.symlink_to("../../valid/.claude-plugin/plugin.json")
    else:
        (tmp_path / "decoy/commands/archive.md").unlink()
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "valid and invalid plugins")
    assert frozen_plugin_tree(tmp_path, "HEAD") == _git(tmp_path, "rev-parse", "HEAD:valid")
    with pytest.raises(HelixHarnessSourceError):
        frozen_plugin_tree(tmp_path, "HEAD", plugin_path="decoy")
