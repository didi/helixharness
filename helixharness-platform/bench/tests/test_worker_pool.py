"""Worker counts follow shared settings; supervision recovers and reaps children."""

import os
import signal
import subprocess
import sys
import time

import pytest

from bench.platform_config import save_platform_config
from bench.task_configuration import task_platform_configuration
from bench.worker_pool import worker_commands


def test_pool_uses_defaults_and_saved_web_configuration():
    defaults = worker_commands(task_platform_configuration(), ["--poll-seconds", "5"])
    assert len(defaults) == 4
    assert sum("FORMAL_EVALUATION" in command for command in defaults) == 2
    assert sum("EVOLUTION_EVALUATION" in command for command in defaults) == 2
    save_platform_config({"resources": {
        "formal_evaluation": {"max_concurrency": 1},
        "evolution_evaluation": {"max_concurrency": 3},
    }})
    configured = worker_commands(task_platform_configuration(), ["--db-path", "/synthetic/shared.db"])
    assert sum("FORMAL_EVALUATION" in command for command in configured) == 1
    assert sum("EVOLUTION_EVALUATION" in command for command in configured) == 3
    assert all(command[-2:] == ["--db-path", "/synthetic/shared.db"] for command in configured)
    assert len({command[command.index("--worker-id") + 1] for command in configured}) == 4


def test_pool_recovers_exited_child_and_stops_survivor(tmp_path):
    record = tmp_path / "child-pids"
    worker = tmp_path / "synthetic_worker.py"
    worker.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]); first=not p.exists()\n"
        "with p.open('a') as f: f.write(str(os.getpid())+'\\n')\n"
        "if first: sys.exit(1)\n"
        "while True: time.sleep(.1)\n"
    )
    command = [sys.executable, str(worker), str(record)]
    supervisor = subprocess.Popen([
        sys.executable, "-c",
        f"from bench.worker_pool import supervise; supervise([{command!r}], .1)",
    ])
    pids = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if record.exists():
                pids = [int(value) for value in record.read_text().splitlines()]
                if len(pids) >= 2:
                    break
            time.sleep(.05)
        assert len(pids) == 2
        assert pids[0] != pids[1]
        os.kill(pids[1], 0)
        supervisor.send_signal(signal.SIGTERM)
        assert supervisor.wait(timeout=15) == 0
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise AssertionError("supervisor left a child process alive")
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=15)


@pytest.mark.parametrize("arguments", [
    ["--on"], ["--workload-k", "FORMAL_EVALUATION"], ["--run-i", "synthetic"],
    ["--worker-id", "override"], ["--once"],
    ["--poll-seconds", "0"], ["--poll-seconds", "nan"], ["--poll-seconds", "inf"],
    ["--restart-delay", "nan"], ["--restart-delay", "inf"],
])
def test_pool_rejects_invalid_arguments_before_starting_workers(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "bench.worker_pool", *arguments],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("kill_worker", [False, True])
def test_pool_cleans_isolated_verifier_on_stop_or_worker_crash(tmp_path, kill_worker):
    record = tmp_path / "verifier-pid"
    worker_record = tmp_path / "worker-pid"
    verifier_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(record)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "from bench.scenario_execution_support import run_frozen_command\n"
        f"p=Path({str(worker_record)!r})\n"
        "if p.exists():\n    time.sleep(60)\n"
        "p.write_text(str(os.getpid()))\n"
        f"run_frozen_command([sys.executable, '-c', {verifier_code!r}], "
        "cwd=Path.cwd(), environment=dict(os.environ), timeout_seconds=60)\n"
    )
    command = [sys.executable, str(worker)]
    pool = subprocess.Popen([
        sys.executable, "-c",
        f"from bench.worker_pool import supervise; supervise([{command!r}], .1)",
    ])
    verifier_pid = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not record.exists():
            time.sleep(.05)
        verifier_pid = int(record.read_text())
        if kill_worker:
            os.kill(int(worker_record.read_text()), signal.SIGKILL)
        else:
            pool.terminate()
            assert pool.wait(timeout=15) == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(verifier_pid, 0)
            except ProcessLookupError:
                break
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(verifier_pid)], capture_output=True, text=True)
            if stat.stdout.strip().startswith("Z"):
                break
            time.sleep(.05)
        else:
            pytest.fail("isolated verifier survived its Worker")
    finally:
        if pool.poll() is None:
            pool.terminate()
            pool.wait(timeout=15)
        if verifier_pid is not None:
            try:
                os.kill(verifier_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
