"""Reject schedule injection and repository paths before touching external state."""

import pytest

from eval import usage_sync
from eval.cli import sync_helixharness_usage as sync


@pytest.mark.parametrize("symlink_kind", ["directory", "feature", "file"])
def test_ledger_consumers_never_follow_escaping_symlinks(tmp_path, symlink_kind):
    import json

    from eval.stage_supplements import _iter_ledger_entries
    from eval.telemetry_quality import TelemetryQualityStore
    from eval.tests.test_usage_loader import _workflow_log
    from eval.usage_loader import discover_dashboard_projects

    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "eval-log.json").write_text(json.dumps(_workflow_log()))
    directory = root / "docs/eval"
    directory.parent.mkdir(parents=True)
    if symlink_kind == "directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        feature = directory / "example"
        if symlink_kind == "feature":
            feature.symlink_to(outside, target_is_directory=True)
        else:
            feature.mkdir()
            (feature / "eval-log.json").symlink_to(outside / "eval-log.json")
    assert discover_dashboard_projects([str(root)]) == []
    assert list(_iter_ledger_entries([root])) == []
    assert TelemetryQualityStore(tmp_path / "quality.db").refresh([root]).discovered == 0
    with pytest.raises(ValueError, match="ledger"):
        sync._eval_inventory(root, "docs/eval")


def test_git_checkout_cannot_introduce_escaping_ledger_directory(tmp_path):
    import subprocess

    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    (source / "docs/eval").symlink_to(tmp_path / "outside", target_is_directory=True)
    for args in (("init", "-q", "-b", "master"), ("add", "."), ("commit", "-qm", "Synthetic symlink")):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    with pytest.raises(ValueError, match="ledger"):
        sync._sync_repo({"name": "repo", "url": source.as_uri()}, {}, tmp_path / "cache", 1, {})


@pytest.mark.parametrize("name", [".", "..", ".demo"])
def test_sync_rejects_reserved_or_escaping_cache_directories(tmp_path, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid cache target reached Git")

    monkeypatch.setattr(sync, "_remote_head", forbidden)
    with pytest.raises(ValueError, match="cache"):
        sync._sync_repo({"name": name, "url": "https://example.test/repo.git"}, {}, tmp_path / "cache", 1, {})


@pytest.mark.parametrize("expression", ["@daily\n@reboot touch /tmp/not-run", "@daily echo wrong", "* * * * $(bad)", "@invalid"])
def test_invalid_cron_never_calls_crontab(tmp_path, monkeypatch, expression):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid schedule reached crontab")

    monkeypatch.setattr(usage_sync.subprocess, "run", forbidden)
    assert "格式不正确" in usage_sync._install_usage_cron(expression)


def test_crontab_read_failure_never_overwrites_existing_schedule(tmp_path, monkeypatch):
    import subprocess

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "crontab: permission denied")

    monkeypatch.setattr(usage_sync, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(usage_sync.subprocess, "run", run)
    message = usage_sync._install_usage_cron("* * * * *")

    assert calls == [["crontab", "-l"]]
    assert "读取" in message and "permission denied" in message


@pytest.mark.parametrize("stage", ["read", "write"])
def test_crontab_timeout_returns_without_enabling_schedule(tmp_path, monkeypatch, stage):
    import subprocess

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert 0 < kwargs.get("timeout", 0) <= 10
        if command == ["crontab", "-l"] and stage == "write":
            return subprocess.CompletedProcess(command, 1, "", "no crontab for synthetic")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def forbidden(value):
        pytest.fail("an unconfirmed installation must not enable the schedule")

    monkeypatch.setattr(usage_sync, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(usage_sync.subprocess, "run", run)
    monkeypatch.setattr(usage_sync, "_save_usage_sync_config", forbidden)
    message = usage_sync._install_usage_cron("* * * * *")
    assert "超时" in message
    if stage == "read":
        assert calls == [["crontab", "-l"]]
        assert "未写入" in message
    else:
        assert calls == [["crontab", "-l"], ["crontab", "-"]]
        assert "核对" in message and "已安装" not in message


@pytest.mark.parametrize("existing", [
    usage_sync.USAGE_CRON_BEGIN + "\n0 * * * * unrelated-job\n",
    usage_sync.USAGE_CRON_END + "\n",
    usage_sync.USAGE_CRON_BEGIN + "\n" + usage_sync.USAGE_CRON_BEGIN + "\n" + usage_sync.USAGE_CRON_END + "\n",
])
def test_malformed_cron_markers_never_remove_other_jobs(tmp_path, monkeypatch, existing):
    import subprocess

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, existing, "")

    monkeypatch.setattr(usage_sync, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(usage_sync.subprocess, "run", run)
    assert "标记不完整" in usage_sync._install_usage_cron("* * * * *")
    assert calls == [["crontab", "-l"]]


@pytest.mark.parametrize("existing", [None, "# Keep this comment\n\n0 * * * * unrelated-job\n\n"])
def test_cron_install_preserves_unrelated_text_and_accepts_absent_table(tmp_path, monkeypatch, existing):
    import subprocess

    writes = []

    def run(command, **kwargs):
        if command == ["crontab", "-l"]:
            return subprocess.CompletedProcess(command, 1 if existing is None else 0, existing or "", "no crontab for synthetic" if existing is None else "")
        writes.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(usage_sync, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(usage_sync.subprocess, "run", run)
    monkeypatch.setattr(usage_sync, "_load_usage_sync_config", lambda: {})
    monkeypatch.setattr(usage_sync, "_save_usage_sync_config", lambda value: None)
    assert "已安装" in usage_sync._install_usage_cron("* * * * *")
    assert writes[0].startswith((existing or "") + usage_sync.USAGE_CRON_BEGIN + "\n")
