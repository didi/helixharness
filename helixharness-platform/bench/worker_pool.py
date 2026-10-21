"""Supervise persistent Worker processes using the Web's saved concurrency settings."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from bench.runtime_secrets import load_runtime_secrets
from bench.task_configuration import TaskPlatformConfiguration, task_platform_configuration
from bench.worker_processes import PROCESS_REGISTRY, kill_group, reap_registered_groups
from bench.workload_policy import EVOLUTION_EVALUATION, FORMAL_EVALUATION


def worker_commands(configuration: TaskPlatformConfiguration, arguments: list[str]) -> list[list[str]]:
    """Resolve both pool sizes once at startup from the shared platform configuration."""
    commands = []
    for lane in (FORMAL_EVALUATION, EVOLUTION_EVALUATION):
        for index in range(1, configuration.resource_policy(lane).max_concurrency + 1):
            commands.append([
                sys.executable, "-m", "bench.scenario_worker",
                "--worker-id", f"pool-{lane.lower()}-{index}",
                "--workload-kind", lane, *arguments,
            ])
    return commands


def supervise(commands: list[list[str]], restart_delay: float = 5.0) -> None:
    """Restart exited children with a delay and reap every child when stopping."""
    stopping = threading.Event()
    previous_handlers = {}
    children: dict[int, subprocess.Popen] = {}
    next_start = [0.0] * len(commands)
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, lambda *_: stopping.set())
    registry = tempfile.TemporaryDirectory(prefix="helixharness-worker-pool-")
    try:
        while not stopping.is_set():
            for index, command in enumerate(commands):
                child = children.get(index)
                if child is not None and child.poll() is not None:
                    reap_registered_groups(Path(registry.name) / str(index))
                    kill_group(child.pid)
                    del children[index]
                    next_start[index] = time.monotonic() + restart_delay
                if index not in children and time.monotonic() >= next_start[index]:
                    directory = Path(registry.name) / str(index)
                    directory.mkdir(exist_ok=True)
                    children[index] = subprocess.Popen(
                        command, start_new_session=True,
                        env={**os.environ, PROCESS_REGISTRY: str(directory)},
                    )
            stopping.wait(0.25)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 10
        for child in children.values():
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for index, child in children.items():
            reap_registered_groups(Path(registry.name) / str(index))
            kill_group(child.pid)
        registry.cleanup()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Start both Worker pools using /framework-evaluation/config concurrency settings; restart to apply changes."
    )
    parser.add_argument("--restart-delay", type=float, default=5.0)
    args, worker_arguments = parser.parse_known_args()
    if not math.isfinite(args.restart_delay) or args.restart_delay <= 0:
        parser.error("--restart-delay must be finite and positive")
    reserved = {"--once", "--worker-id", "--workload-kind", "--run-id"}
    if any(argument.split("=", 1)[0] in reserved for argument in worker_arguments):
        parser.error("pool controls worker IDs and workloads; one-shot or Run-specific options are not supported")
    # Validate forwarded arguments once rather than restarting invalid workers forever.
    from bench.scenario_worker import _parser

    worker_parser = _parser()
    worker_parser.allow_abbrev = False
    worker_args = worker_parser.parse_args(worker_arguments)
    if not math.isfinite(worker_args.poll_seconds) or worker_args.poll_seconds <= 0:
        parser.error("--poll-seconds must be finite and positive")
    load_runtime_secrets()
    configuration = task_platform_configuration()
    print(json.dumps({
        "formal_workers": configuration.formal_evaluation.max_concurrency,
        "evolution_workers": configuration.evolution_evaluation.max_concurrency,
        "configuration_changes": "restart pool to apply",
    }), flush=True)
    supervise(worker_commands(configuration, worker_arguments), args.restart_delay)


if __name__ == "__main__":
    main()
