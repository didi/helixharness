"""Independent worker process for isolated Scenario v2 execution jobs."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from bench.helixharness_source import HelixHarnessSourceError, git_repository_root
from bench.runtime_secrets import load_runtime_secrets
from bench.scenario_codex import CodexScenarioExecutionAdapter
from bench.scenario_runtime import MockScenarioExecutionAdapter, ScenarioWorker
from bench.scenario_storage import ScenarioStore
from bench.task_configuration import task_platform_configuration
from bench.workload_policy import FORMAL_EVALUATION, SCENARIO_EXECUTION_WORKLOADS


def parse_repositories(values: list[str]) -> dict[str, Path]:
    """Parse explicit ``repository_id=/absolute/path`` mappings."""

    repositories: dict[str, Path] = {}
    for value in values:
        repository_id, separator, raw_path = value.partition("=")
        if not separator or not repository_id.strip() or not raw_path.strip():
            raise ValueError("Repository mappings must use repository_id=/absolute/path")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_absolute() or not (path / ".git").exists():
            raise ValueError(f"Repository mapping is not a Git checkout: {repository_id}")
        if repository_id in repositories:
            raise ValueError(f"Duplicate repository mapping: {repository_id}")
        repositories[repository_id] = path
    return repositories


def cached_repositories(root: Path) -> dict[str, Path]:
    """Reuse the Bench-owned Git cache without consulting the v1 SQLite store."""

    root = root.resolve()
    if not root.is_dir():
        return {}
    return {
        path.name: path.resolve()
        for path in sorted(root.iterdir())
        if path.is_dir() and (path / ".git").exists()
    }


def _repository_values(cli_values: list[str]) -> list[str]:
    configured = os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORIES", "")
    environment_values = [item.strip() for item in configured.split(",") if item.strip()]
    return [*environment_values, *cli_values]


def worker_workload_kind(worker_id: str, explicit: str | None) -> str:
    """Resolve managed instance names while keeping local workers formal by default."""

    if explicit is not None:
        return explicit
    return next(
        (workload_kind for workload_kind in SCENARIO_EXECUTION_WORKLOADS if workload_kind in worker_id),
        FORMAL_EVALUATION,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HelixHarness Scenario v2 worker")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", "bench/.cache/evaluation.db")),
    )
    parser.add_argument("--worker-id", default="scenario-worker")
    parser.add_argument("--adapter", choices=("CODEX", "MOCK"), default="CODEX")
    parser.add_argument(
        "--workload-kind",
        choices=SCENARIO_EXECUTION_WORKLOADS,
        default=None,
        help="Only consume the selected workload FIFO",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--helixharness-repository",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_WORK_ROOT", "bench/.cache/scenario-work")),
    )
    parser.add_argument("--repository", action="append", default=[], help="repository_id=/absolute/path")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORY_ROOT", "bench/.cache/repositories")),
        help="Bench-owned Git cache; explicit --repository mappings override matching IDs",
    )
    return parser


def _terminate_worker(*_: object) -> None:
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _terminate_worker)
    args = _parser().parse_args()
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")
    load_runtime_secrets()
    store = ScenarioStore(args.db_path.resolve())
    store.initialize()
    if args.adapter == "MOCK":
        adapter = MockScenarioExecutionAdapter()
    else:
        try:
            repositories = {
                **cached_repositories(args.repository_root),
                **parse_repositories(_repository_values(args.repository)),
            }
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        try:
            helixharness_repository = git_repository_root(
                args.helixharness_repository or Path(task_platform_configuration().helixharness_repository)
            )
        except HelixHarnessSourceError as exc:
            raise SystemExit(str(exc)) from exc
        adapter = CodexScenarioExecutionAdapter(
            helixharness_repository=helixharness_repository,
            repositories=repositories,
            work_root=args.work_root,
            workflow_progress_recorder=store.record_job_workflow_progress,
        )
    worker = ScenarioWorker(
        store,
        adapter,
        worker_id=args.worker_id,
        workload_kind=worker_workload_kind(args.worker_id, args.workload_kind),
    )
    while True:
        result = worker.run_once(run_id=args.run_id)
        sys.stdout.write(json.dumps({"status": result.status, "job_id": result.job_id}) + "\n")
        sys.stdout.flush()
        if args.once:
            return
        if result.status in {"IDLE", "RETRY_PENDING"}:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
