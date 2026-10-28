#!/usr/bin/env python3
"""Operator CLI for HelixHarness async telemetry status, delivery, backfill, and repair."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import telemetry_async as telemetry_async
from telemetry_hosts import supported_host_ids


def _duration(value: str) -> timedelta:
    text = value.strip().lower()
    if text.endswith("d") and text[:-1].isdigit():
        return timedelta(days=int(text[:-1]))
    if text.endswith("h") and text[:-1].isdigit():
        return timedelta(hours=int(text[:-1]))
    raise argparse.ArgumentTypeError("duration must use Nd or Nh")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run")
    run.add_argument("--all", action="store_true")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--since", type=_duration, default=timedelta(days=7))
    scope = backfill.add_mutually_exclusive_group()
    scope.add_argument("--repo")
    scope.add_argument("--all", action="store_true")
    repair = sub.add_parser("repair")
    repair.add_argument("--repo")
    repair.add_argument("--platform", choices=supported_host_ids())
    return parser


def main() -> int:
    args = _parser().parse_args()
    home = telemetry_async.telemetry_home(args.home)
    outbox = telemetry_async.Outbox(home)
    exit_code = 0
    if args.command == "status":
        pending = len(outbox.pending())
        corrupt = outbox.last_corrupt_count
        runtime_installed = (home / "runtime-manifest.json").is_file()
        scheduler = telemetry_async.scheduler_status(home)
        runtime_health = telemetry_async.runtime_health_status(home)
        result = {
            "home": str(home),
            "pending": pending,
            "quarantined": outbox.quarantine_count(),
            "corrupt_discovered": corrupt,
            "registered_repositories": telemetry_async.registered_repositories(home),
            "runtime_installed": runtime_installed,
            "scheduler": scheduler,
            "runtime_health": runtime_health,
        }
        exit_code = 1 if pending or corrupt or not runtime_installed or not scheduler.get("healthy") or runtime_health.get("circuit_open") or runtime_health.get("collection_circuits_open") or runtime_health.get("delivery_circuits_open") else 0
    elif args.command == "run":
        result = telemetry_async.run_once(home)
        exit_code = 1 if result.get("status") != "skipped" and (result["collection_failed"] or result["remaining"] or result["rejected"] or result["quarantined"] or result.get("circuit_open") or result.get("collection_circuits_open") or result.get("delivery_circuits_open")) else 0
    elif args.command == "repair":
        try:
            manifest = json.loads((home / "runtime-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        plugin_root = Path(str(manifest.get("plugin_root") or SCRIPT_DIR.parent)).expanduser().resolve()
        telemetry_async.install_runtime(plugin_root, home)
        if args.repo and args.platform:
            telemetry_async.register_repository(Path(args.repo), args.platform, home)
        result = telemetry_async.ensure_scheduler(home)
        if result.get("healthy"):
            result["runtime_health"] = telemetry_async.reset_runtime_health(home)
        exit_code = 1 if not result.get("healthy") else 0
    else:
        cutoff = datetime.now(timezone.utc) - args.since
        repositories = telemetry_async.registered_repositories(home)
        if args.repo:
            repositories = [item for item in repositories if Path(str(item["path"])).resolve() == Path(args.repo).resolve()]
        # Backfill never scans arbitrary host history. It revisits only exact
        # locators retained by registered repositories, then retries matching
        # durable Outbox records.
        result = {
            "since": cutoff.isoformat().replace("+00:00", "Z"),
            "repositories": len(repositories),
            "delivery": telemetry_async.run_once(
                home,
                repo_filter={Path(str(item["path"])).resolve() for item in repositories},
                source_since=cutoff.isoformat().replace("+00:00", "Z"),
            ),
            "unrecoverable": 0,
        }
        result["unrecoverable"] = result["delivery"]["collection_failed"]
        exit_code = 1 if result["delivery"].get("status") != "skipped" and (result["unrecoverable"] or result["delivery"]["remaining"] or result["delivery"]["rejected"] or result["delivery"]["quarantined"] or result["delivery"].get("collection_circuits_open") or result["delivery"].get("delivery_circuits_open")) else 0
    report_path = home / "reports" / f"telemetryctl-{args.command}.json"
    telemetry_async._atomic_bytes(report_path, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"ok": exit_code == 0, **result, "report_path": str(report_path)}, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
