"""Operator CLI for deterministic Scenario v2 repository calibration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from bench.scenario_calibration import ScenarioCalibrationError, ScenarioCalibrationService
from bench.scenario_storage import ScenarioStore
from bench.scenario_worker import cached_repositories, parse_repositories
from bench.task_configuration import task_platform_configuration


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate published repository Scenarios")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", "bench/.cache/evaluation.db")),
    )
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="Calibrate every current REPOSITORY_CASE")
    parser.add_argument("--repository", action="append", default=[], help="repository_id=/absolute/path")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORY_ROOT", "bench/.cache/repositories")),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.all and not args.case_id:
        raise SystemExit("Use --case-id or --all")
    configured = [item.strip() for item in os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORIES", "").split(",") if item.strip()]
    try:
        repositories = {
            **cached_repositories(args.repository_root),
            **parse_repositories([*configured, *args.repository]),
        }
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    store = ScenarioStore(args.db_path.resolve())
    store.initialize()
    case_ids = (
        [item.case_id for item in store.list_current() if item.case_kind == "REPOSITORY_CASE"]
        if args.all
        else args.case_id
    )
    policy = task_platform_configuration().case_calibration
    concurrency = policy.max_concurrency if args.concurrency is None else args.concurrency
    if concurrency < 1 or concurrency > 32:
        raise SystemExit("--concurrency must be between 1 and 32")
    service = ScenarioCalibrationService(
        store,
        repositories=repositories,
        wall_timeout_seconds=policy.wall_timeout_seconds,
        disk_budget_mb=policy.disk_budget_mb,
        gradle_heap_mb=policy.gradle_heap_mb,
    )
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(service.calibrate, case_ids))
    except ScenarioCalibrationError as exc:
        sys.stderr.write(f"calibration blocked: {exc}\n")
        raise SystemExit(2) from exc
    sys.stdout.write(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2) + "\n")
    if any(result.status != "CALIBRATED" for result in results):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
