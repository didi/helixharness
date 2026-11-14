"""Install a per-user Windows sync task and evaluate its saved cron schedule."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import yaml
from croniter import croniter

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_definition(config: Path, cache: Path, project: Path) -> tuple[str, str]:
    """Build an interactive-user task without passwords or administrator privileges."""
    name = "HelixHarnessPlatform-UsageSync-" + hashlib.sha256(str(config.resolve()).encode()).hexdigest()[:16]
    arguments = subprocess.list2cmdline(["-X", "utf8", "-m", "eval.windows_schedule", "--config", str(config.resolve()),
                                        "--cache-dir", str(cache.resolve())])
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    user = escape(f"{domain}\\{username}" if domain else username)
    start = datetime.now().replace(second=0, microsecond=0).isoformat()
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{NAMESPACE}">
  <RegistrationInfo><Description>HelixHarness Platform usage sync</Description></RegistrationInfo>
  <Triggers><TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
  </TimeTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><Enabled>true</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(sys.executable)}</Command>
    <Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(project.resolve()))}</WorkingDirectory>
  </Exec></Actions>
</Task>'''
    return name, xml


def install(config: Path, cache: Path, project: Path, expression: str) -> str:
    """Create only this installation's task and verify the stored action before success."""
    if expression == "@reboot" or not croniter.is_valid(expression):
        raise ValueError("Windows 定时同步需要有效的五字段 cron 或 @hourly/@daily 等时间表达式，不支持 @reboot")
    name, xml = task_definition(config, cache, project)
    with tempfile.TemporaryDirectory(prefix="helix-schedule-") as temporary:
        definition = Path(temporary) / "task.xml"
        definition.write_bytes(xml.encode("utf-16"))
        created = subprocess.run(["schtasks.exe", "/Create", "/TN", name, "/XML", str(definition), "/F"],
                                 capture_output=True, timeout=15, check=False)
        if created.returncode:
            raise OSError(f"Windows task creation failed (exit {created.returncode})")
    checked = subprocess.run(["schtasks.exe", "/Query", "/TN", name, "/XML"],
                             capture_output=True, timeout=15, check=False)
    if checked.returncode:
        raise OSError("Windows task was submitted but readback failed; inspect Task Scheduler before retrying")
    try:
        # schtasks XML output encoding varies with the Windows console code page.
        content = checked.stdout
        if isinstance(content, bytes) and not content.startswith((b"\xff\xfe", b"\xfe\xff")):
            content = content.decode("utf-16-le" if b"\x00" in content[:100] else "utf-8-sig")
        actual = ElementTree.fromstring(content)
        expected = ElementTree.fromstring(xml)
        for query in ("Actions/Exec/Command", "Actions/Exec/Arguments", "Actions/Exec/WorkingDirectory",
                      "Settings/MultipleInstancesPolicy", "Triggers/TimeTrigger/Repetition/Interval"):
            path = "/".join(f"{{{NAMESPACE}}}{part}" for part in query.split("/"))
            if actual.findtext(path) != expected.findtext(path):
                raise ValueError("Windows task readback differs from the requested definition")
        # Task Scheduler omits boolean nodes that equal their schema default.
        # Missing Enabled therefore means true; an explicit false still fails closed.
        for query in ("Settings/Enabled", "Triggers/TimeTrigger/Enabled"):
            path = "/".join(f"{{{NAMESPACE}}}{part}" for part in query.split("/"))
            enabled = actual.findtext(path)
            if enabled is not None and enabled.strip().lower() != "true":
                raise ValueError("Windows task readback differs from the requested definition")
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise OSError("Windows task readback is not valid XML") from exc
    return name


def run_due(config: Path, cache: Path, *, now: datetime | None = None) -> int:
    """One Task Scheduler tick; do nothing unless the saved schedule is enabled and due."""
    document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    schedule = document.get("schedule") or {}
    moment = (now or datetime.now()).replace(second=0, microsecond=0)
    if not schedule.get("enabled") or not croniter.match(schedule.get("cron", "0 * * * *"), moment):
        return 0
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / ("windows-schedule-" + hashlib.sha256(str(config.resolve()).encode()).hexdigest()[:16] + ".txt")
    identity = moment.isoformat()
    if marker.is_file() and marker.read_text(encoding="utf-8") == identity:
        return 0
    # Task Scheduler IgnoreNew serializes invocations of this task.
    marker.write_text(identity, encoding="utf-8")
    script = Path(__file__).resolve().parent / "cli/sync_helixharness_usage.py"
    with (cache / "scheduled-sync.log").open("ab") as output:
        return subprocess.run([sys.executable, "-X", "utf8", str(script), "--config", str(config), "--cache-dir", str(cache)],
                              stdout=output, stderr=subprocess.STDOUT, check=False, timeout=7100).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run_due(args.config, args.cache_dir))
