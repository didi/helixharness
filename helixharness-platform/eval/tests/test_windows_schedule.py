from datetime import datetime
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
import yaml

from eval import windows_schedule


def test_installer_verifies_xml_and_uses_parameter_arrays(tmp_path, monkeypatch):
    config = tmp_path / "space 中文" / "sync.yaml"
    captured = []
    xml = None

    def run(command, **kwargs):
        nonlocal xml
        captured.append(command)
        if "/Create" in command:
            from pathlib import Path
            xml = Path(command[command.index("/XML") + 1]).read_bytes()
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=xml)

    monkeypatch.setattr(windows_schedule.subprocess, "run", run)
    name = windows_schedule.install(config, tmp_path / "cache", tmp_path, "*/15 * * * *")
    assert name.startswith("HelixHarnessPlatform-UsageSync-")
    assert len(captured) == 2
    assert captured[0][4] == "/XML"
    assert "中文" in xml.decode("utf-16")
    assert "InteractiveToken" in xml.decode("utf-16")


def test_installer_accepts_task_scheduler_omitted_default_enabled_nodes(tmp_path, monkeypatch):
    """Task Scheduler normalizes explicit true values by omitting default nodes."""
    stored = None

    def run(command, **kwargs):
        nonlocal stored
        if "/Create" in command:
            from pathlib import Path

            requested = ElementTree.fromstring(Path(command[command.index("/XML") + 1]).read_bytes())
            namespace = f"{{{windows_schedule.NAMESPACE}}}"
            settings = requested.find(f"{namespace}Settings")
            settings.remove(settings.find(f"{namespace}Enabled"))
            trigger = requested.find(f"{namespace}Triggers/{namespace}TimeTrigger")
            trigger.remove(trigger.find(f"{namespace}Enabled"))
            stored = ElementTree.tostring(requested, encoding="utf-16", xml_declaration=True)
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=stored)

    monkeypatch.setattr(windows_schedule.subprocess, "run", run)
    assert windows_schedule.install(tmp_path / "sync.yaml", tmp_path / "cache", tmp_path, "@hourly")


def test_installer_rejects_an_explicitly_disabled_stored_task(tmp_path, monkeypatch):
    stored = None

    def run(command, **kwargs):
        nonlocal stored
        if "/Create" in command:
            from pathlib import Path

            requested = ElementTree.fromstring(Path(command[command.index("/XML") + 1]).read_bytes())
            namespace = f"{{{windows_schedule.NAMESPACE}}}"
            requested.find(f"{namespace}Settings/{namespace}Enabled").text = "false"
            stored = ElementTree.tostring(requested, encoding="utf-16", xml_declaration=True)
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=stored)

    monkeypatch.setattr(windows_schedule.subprocess, "run", run)
    with pytest.raises(ValueError, match="readback differs"):
        windows_schedule.install(tmp_path / "sync.yaml", tmp_path / "cache", tmp_path, "@hourly")


def test_unverified_schedule_is_not_reported_as_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_schedule.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(OSError, match="creation failed"):
        windows_schedule.install(tmp_path / "sync.yaml", tmp_path / "cache", tmp_path, "@hourly")


def test_ticks_respect_enabled_due_and_same_minute(tmp_path, monkeypatch):
    config = tmp_path / "sync.yaml"
    calls = []
    monkeypatch.setattr(windows_schedule.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    cache = tmp_path / "cache"
    config.write_text(yaml.safe_dump({"schedule": {"enabled": False, "cron": "0 * * * *"}}))
    windows_schedule.run_due(config, cache, now=datetime(2026, 1, 1, 10, 0))
    assert not calls
    config.write_text(yaml.safe_dump({"schedule": {"enabled": True, "cron": "0 * * * *"}}))
    windows_schedule.run_due(config, cache, now=datetime(2026, 1, 1, 10, 1))
    assert not calls
    for _ in range(2):
        windows_schedule.run_due(config, cache, now=datetime(2026, 1, 1, 11, 0))
    assert len(calls) == 1


def test_route_dispatches_windows_and_saves_only_after_readback(tmp_path, monkeypatch):
    from eval import usage_sync

    monkeypatch.setattr(usage_sync.system_platform, "system", lambda: "Windows")
    monkeypatch.setattr(usage_sync, "_load_usage_sync_config", lambda: {})
    saved = []
    monkeypatch.setattr(usage_sync, "_save_usage_sync_config", saved.append)
    monkeypatch.setattr(windows_schedule, "install", lambda *a: "synthetic-task")
    assert "已安装 Windows" in usage_sync._install_usage_cron("0 * * * *")
    assert saved == [{"schedule": {"cron": "0 * * * *", "enabled": True}}]
    saved.clear()

    def fail(*args):
        raise OSError("synthetic readback failure")

    monkeypatch.setattr(windows_schedule, "install", fail)
    assert "未确认启用" in usage_sync._install_usage_cron("0 * * * *")
    assert not saved
