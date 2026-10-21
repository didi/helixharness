from __future__ import annotations

import os
import io
import json
import tempfile
import threading
import unittest
from unittest import mock
import xml.etree.ElementTree as element_tree
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

import reporting_config
import telemetry_async as telemetry_async
import telemetry_collector as telemetry_collector
import telemetry_ledger as telemetry_ledger
import telemetry_trace as telemetry_trace
import telemetryctl as telemetryctl


def _supplement(identifier: str) -> dict:
    return {
        "schema": "helixharness-stage-supplement/1.0",
        "supplement_id": identifier,
        "revision": 1,
        "supersedes": None,
        "entry_id": "entry-0123456789abcdef",
        "requirement_id": "req_0123456789abcdef01234567",
        "requirement_name": "checkout-timeout",
        "feature": "checkout-timeout",
        "session_hash": "session-123",
        "platform": "codex",
        "collected_at": "2026-08-12T01:02:00Z",
        "usage": {},
        "evidence": {"schema": "helixharness-evolution-evidence/1.0"},
    }


class TelemetryAsyncV2Test(unittest.TestCase):
    def setUp(self) -> None:
        endpoint_patch = mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "http://127.0.0.1:9/report")
        endpoint_patch.start()
        self.addCleanup(endpoint_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_endpoint_gate_stops_collection_delivery_and_runtime_side_effects(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        outbox.enqueue(_supplement("supp_0123456789abcdef"), repo=Path("/tmp/repo"),
                       platform="codex", consent_epoch="cep_test", endpoint="http://example.test/report")
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        for endpoint, reason in (("", "endpoint_unconfigured"), ("https://host:bad/report", "endpoint_invalid")):
            with self.subTest(endpoint=endpoint), mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", endpoint), \
                 mock.patch.object(telemetry_async, "_run_once_unlocked") as worker, \
                 mock.patch.object(telemetry_async, "install_runtime") as install, \
                 mock.patch.object(telemetry_async.subprocess, "Popen") as spawn, \
                 mock.patch.object(telemetry_collector, "_collect_entry") as collect:
                sender = mock.Mock()
                report = telemetry_async.run_once(self.home, sender=sender, health_sender=sender)
                self.assertEqual(reason, report["reason"])
                self.assertEqual(0, report["collection_failed"])
                self.assertEqual(reason, telemetry_async.ensure_runtime_ready(Path("/missing"), Path("/repo"), "codex", self.home)["reason"])
                self.assertFalse(telemetry_async.wake_detached(self.home))
                self.assertEqual(0, telemetry_collector.collect_finished(Path("/repo"), home=self.home)["failed"])
                self.assertEqual(reason, telemetry_collector.collect_entry(Path("/repo"), "entry-test", home=self.home)["reason"])
                for operation in (worker, install, spawn, collect, sender):
                    operation.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()})
        sender = mock.Mock(return_value=[{"supplement_id": "supp_0123456789abcdef", "status": "accepted"}])
        report = telemetry_async.run_once(
            self.home, sender=sender,
            consent_resolver=lambda *_args: {"effective_mode": "enabled", "consent_epoch": "cep_test"},
        )
        self.assertEqual(1, report["accepted"])
        self.assertEqual("http://example.test/report", sender.call_args.args[0])

    def test_telemetryctl_skips_without_endpoint(self) -> None:
        for command in ("run", "backfill"):
            with self.subTest(command=command), mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", ""), \
                 mock.patch("sys.argv", ["telemetryctl", "--home", str(self.home), command, "--all"]), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(0, telemetryctl.main())
                result = json.loads(output.getvalue())
                delivery = result if command == "run" else result["delivery"]
                self.assertEqual("endpoint_unconfigured", delivery["reason"])
                self.assertEqual("skipped", delivery["status"])

    def test_endpoint_configuration_errors_and_precedence(self) -> None:
        config = self.home / "config.json"
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", ""):
            for raw in ('{broken', '[]', '{"endpoint":42}', '{"endpoint":"https://host:0/report"}'):
                config.write_text(raw)
                self.assertEqual("endpoint_invalid", telemetry_async.endpoint_status(self.home)["reason"])
            config.write_text('{"endpoint":"http://example.test/report"}')
            self.assertTrue(telemetry_async.endpoint_status(self.home)["ready"])
        config.write_text('{broken')
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "http://published.example/report"):
            self.assertEqual("endpoint_invalid", telemetry_async.endpoint_status(self.home)["reason"])

    def test_partial_ack_removes_only_success_and_quarantines_permanent_reject(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        accepted = _supplement("supp_0123456789abcdef")
        retryable = _supplement("supp_1111111111111111")
        rejected = _supplement("supp_2222222222222222")
        for item in (accepted, retryable, rejected):
            outbox.enqueue(
                item,
                repo=Path("/tmp/repo"),
                platform="codex",
                consent_epoch="cep_0123456789abcdef01234567",
                endpoint="http://example.test/report",
            )

        def sender(_endpoint: str, items: list[dict]) -> list[dict]:
            self.assertEqual(3, len(items))
            return [
                {"supplement_id": accepted["supplement_id"], "status": "accepted"},
                {"supplement_id": retryable["supplement_id"], "status": "retryable"},
                {"supplement_id": rejected["supplement_id"], "status": "rejected"},
            ]

        report = telemetry_async.run_once(
            self.home,
            sender=sender,
            consent_resolver=lambda *_: {
                "effective_mode": "enabled",
                "consent_epoch": "cep_0123456789abcdef01234567",
            },
        )
        self.assertEqual(1, report["accepted"])
        self.assertEqual(1, report["retryable"])
        self.assertEqual(1, report["rejected"])
        self.assertEqual([retryable["supplement_id"]], [item["supplement_id"] for item in outbox.pending()])
        self.assertFalse((self.home / "accepted" / f"{accepted['supplement_id']}.json.gz").exists())
        self.assertTrue((self.home / "quarantine" / f"{rejected['supplement_id']}.json.gz").is_file())

    def test_final_duplicate_uses_delivered_cleanup(self) -> None:
        repo = self.home / "duplicate-cleanup"
        repo.mkdir()
        item = _supplement("supp_3333333333333333")
        telemetry_async.Outbox(self.home).enqueue(
            item,
            repo=repo,
            platform="codex",
            consent_epoch="cep_0123456789abcdef01234567",
            final_for_entry=True,
        )
        with mock.patch.object(
            telemetry_ledger,
            "cleanup_delivered_entry",
            return_value=5,
        ) as cleanup:
            report = telemetry_async.run_once(
                self.home,
                sender=lambda *_: [
                    {"supplement_id": item["supplement_id"], "status": "duplicate"}
                ],
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
                consent_lease_factory=lambda *_: nullcontext({}),
            )

        self.assertEqual(1, report["duplicate"])
        self.assertEqual([], telemetry_async.Outbox(self.home).pending())
        cleanup.assert_called_once_with(
            repo.resolve(),
            item["entry_id"],
            feature=item["feature"],
        )

    def test_delivery_batches_by_compressed_size_and_pauses_between_requests(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        for index in range(3):
            item = _supplement(f"supp_{index:016x}")
            item["evidence"]["user_inputs"] = [f"requirement-{index}-" + ("x" * 4_000)]
            outbox.enqueue(
                item,
                repo=Path("/tmp/repo"),
                platform="codex",
                consent_epoch="cep_0123456789abcdef01234567",
                endpoint="http://example.test/report",
            )
        batches: list[list[str]] = []
        pauses: list[float] = []

        def sender(_endpoint: str, items: list[dict]) -> list[dict]:
            batches.append([item["supplement_id"] for item in items])
            return [
                {"supplement_id": item["supplement_id"], "status": "accepted"}
                for item in items
            ]

        report = telemetry_async.run_once(
            self.home,
            sender=sender,
            consent_resolver=lambda *_: {
                "effective_mode": "enabled",
                "consent_epoch": "cep_0123456789abcdef01234567",
            },
            max_batch_compressed_bytes=200,
            batch_pause_seconds=0.25,
            sleep_fn=pauses.append,
        )

        self.assertEqual(3, report["accepted"])
        self.assertEqual(3, len(batches))
        self.assertEqual([0.25, 0.25], pauses)
        self.assertEqual([], outbox.pending())

    def test_runtime_endpoint_can_be_overridden_by_private_config(self) -> None:
        telemetry_async._atomic_bytes(
            self.home / "config.json",
            b'{"endpoint":"http://telemetry.internal/report-v2"}\n',
        )
        self.assertEqual(
            "http://telemetry.internal/report-v2",
            telemetry_async.configured_endpoint(self.home),
        )
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "https://published.example/report"):
            self.assertEqual(
                "http://telemetry.internal/report-v2",
                telemetry_async.configured_endpoint(self.home),
            )


    def test_tampered_workflow_trace_is_not_forwarded(self) -> None:
        repo = self.home / "tampered-trace"
        trace_path = (
            repo
            / ".helixharness"
            / "telemetry-v2"
            / "traces"
            / "entry-0123456789abcdef.json"
        )
        trace_path.parent.mkdir(parents=True)
        trace_path.write_text(
            json.dumps(
                {
                    "schema": "helixharness-workflow-trace/1.0",
                    "status": "collected",
                    "secret_free_text": "must-not-leave-repo",
                }
            ),
            encoding="utf-8",
        )

        loaded = telemetry_trace.load_for_supplement(
            repo,
            "entry-0123456789abcdef",
        )

        self.assertEqual("unavailable", loaded["status"])
        self.assertNotIn("must-not-leave-repo", json.dumps(loaded))

    def test_run_reports_remaining_deliveries_after_backoff(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        item = _supplement("supp_6666666666666666")
        outbox.enqueue(
            item,
            repo=Path("/tmp/repo"),
            platform="codex",
            consent_epoch="cep_0123456789abcdef01234567",
        )
        report = telemetry_async.run_once(
            self.home,
            sender=lambda *_: [{"supplement_id": item["supplement_id"], "status": "retryable"}],
            consent_resolver=lambda *_: {
                "effective_mode": "enabled",
                "consent_epoch": "cep_0123456789abcdef01234567",
            },
        )
        self.assertEqual(1, report["retryable"])
        self.assertEqual(1, report["remaining"])

    def test_run_reports_exact_locator_collection_failures(self) -> None:
        repo = self.home / "registered-repo"
        repo.mkdir()
        ledger = repo / "docs" / "eval" / "lite-feature" / "eval-log.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            '{"schema":"helixharness-eval-ledger/1.0","schema_version":"1.0","entries":[]}\n',
            encoding="utf-8",
        )
        telemetry_async.register_repository(repo, "codex", self.home)
        with mock.patch.object(
            telemetry_collector,
            "collect_finished",
            return_value={"collected": 0, "skipped": 0, "failed": 2},
        ):
            report = telemetry_async.run_once(
                self.home,
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
            )
        self.assertEqual(2, report["collection_failed"])
        self.assertEqual(0, report["remaining"])

    def test_corrupt_outbox_is_reported_as_unrecovered(self) -> None:
        path = self.home / "outbox" / "supp_corrupt.json.gz"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not-gzip")
        report = telemetry_async.run_once(self.home)
        self.assertEqual(0, report["collection_failed"])
        self.assertEqual(1, report["corrupt_quarantined"])
        self.assertEqual(1, report["quarantined"])


    def test_scheduler_install_failure_is_not_reported_as_repaired(self) -> None:
        executable = self.home / "bin" / "telemetryctl"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        plist = self.home / "launchd" / "com.helixharness.telemetry.plist"
        failed = mock.Mock(returncode=1)
        with mock.patch.dict(os.environ, {"HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": ""}), mock.patch.object(telemetry_async.system_platform, "system", return_value="Darwin"), mock.patch.object(
            telemetry_async, "_launchd_path", return_value=plist
        ), mock.patch.object(telemetry_async.subprocess, "run", return_value=failed):
            result = telemetry_async.ensure_scheduler(self.home)
        self.assertFalse(result["healthy"])
        self.assertEqual("launchd_bootstrap_failed", result["error"])

    def test_macos_scheduler_recovers_at_login_and_keeps_ten_minute_fallback(self) -> None:
        executable = self.home / "bin" / "telemetryctl"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        plist = self.home / "launchd" / "com.helixharness.telemetry.plist"
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            telemetry_async.system_platform, "system", return_value="Darwin"
        ), mock.patch.object(
            telemetry_async, "_launchd_path", return_value=plist
        ), mock.patch.object(
            telemetry_async.subprocess, "run", return_value=completed
        ):
            result = telemetry_async.ensure_scheduler(self.home)

        self.assertTrue(result["healthy"])
        content = plist.read_text(encoding="utf-8")
        self.assertIn("<key>RunAtLoad</key><true/>", content)
        self.assertIn("<key>StartInterval</key><integer>600</integer>", content)

    def test_windows_scheduler_uses_ignore_new_single_instance_policy(self) -> None:
        executable = self.home / "runtime" / "telemetryctl.py"
        executable.parent.mkdir(parents=True)
        executable.write_text("raise SystemExit(0)\n", encoding="utf-8")
        def task_command(arguments: list[str], **_kwargs):
            if "/Query" in arguments:
                return mock.Mock(
                    returncode=0,
                    stdout=telemetry_async._windows_task_xml(self.home),
                )
            return mock.Mock(returncode=0)

        with mock.patch.object(
            telemetry_async.system_platform, "system", return_value="Windows"
        ), mock.patch.object(
            telemetry_async.subprocess, "run", side_effect=task_command
        ) as run:
            result = telemetry_async.ensure_scheduler(self.home)

        self.assertTrue(result["healthy"])
        self.assertEqual("windows-task-scheduler", result["scheduler"])
        task_xml = self.home / "scheduler" / "helixharness-telemetry.xml"
        content = task_xml.read_text(encoding="utf-16")
        element_tree.parse(task_xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", content)
        self.assertIn("<ExecutionTimeLimit>PT15M</ExecutionTimeLimit>", content)
        self.assertIn("<Interval>PT10M</Interval>", content)
        self.assertLess(content.index("<StartBoundary>"), content.index("<Repetition>"))
        self.assertIn("<UserId>", content)
        self.assertIn("<LogonTrigger>", content)
        self.assertEqual("schtasks", run.call_args_list[0].args[0][0])
        self.assertIn("/Create", run.call_args_list[0].args[0])

    def test_windows_scheduler_rejects_disabled_or_stale_task_definition(self) -> None:
        executable = self.home / "runtime" / "telemetryctl.py"
        executable.parent.mkdir(parents=True)
        executable.write_text("raise SystemExit(0)\n", encoding="utf-8")
        stale = mock.Mock(
            returncode=0,
            stdout=(
                '<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                '<Settings><Enabled>false</Enabled><MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy></Settings>'
                '<Actions><Exec><Command>C:\\stale.exe</Command><Arguments>stale</Arguments></Exec></Actions>'
                '</Task>'
            ),
        )

        with mock.patch.object(
            telemetry_async.system_platform, "system", return_value="Windows"
        ), mock.patch.object(
            telemetry_async.subprocess, "run", return_value=stale
        ):
            status = telemetry_async.scheduler_status(self.home)

        self.assertFalse(status["healthy"])

    def test_windows_scheduler_removal_deletes_task_and_private_definition(self) -> None:
        task_xml = self.home / "scheduler" / "helixharness-telemetry.xml"
        task_xml.parent.mkdir(parents=True)
        task_xml.write_text("private task definition", encoding="utf-8")
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            telemetry_async.system_platform, "system", return_value="Windows"
        ), mock.patch.object(
            telemetry_async.subprocess, "run", return_value=completed
        ) as run:
            result = telemetry_async.remove_scheduler(self.home)

        self.assertTrue(result["removed"])
        self.assertFalse(task_xml.exists())
        self.assertEqual(
            ["schtasks", "/Delete", "/TN", telemetry_async.WINDOWS_TASK_NAME, "/F"],
            run.call_args.args[0],
        )

    def test_overlapping_worker_skips_without_collecting_or_sending(self) -> None:
        with telemetry_async._worker_lease(self.home) as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(
                telemetry_collector, "collect_finished"
            ) as collect, mock.patch.object(
                telemetry_async, "_http_sender"
            ) as sender:
                report = telemetry_async.run_once(self.home)

        self.assertEqual(1, report["skipped_existing_worker"])
        self.assertEqual(0, report["collection_failed"])
        collect.assert_not_called()
        sender.assert_not_called()

    def test_six_runtime_exceptions_open_global_circuit_and_report_health_once(self) -> None:
        repo = self.home / "circuit-repo"
        repo.mkdir()
        telemetry_async.register_repository(repo, "codex", self.home)
        health_events: list[dict] = []
        with mock.patch.object(
            telemetry_async,
            "_run_once_unlocked",
            side_effect=OSError("runtime unavailable"),
        ) as worker:
            for _ in range(telemetry_async.CIRCUIT_FAILURE_THRESHOLD):
                with self.assertRaises(OSError):
                    telemetry_async.run_once(
                        self.home,
                        consent_resolver=lambda *_: {
                            "effective_mode": "enabled",
                            "consent_epoch": "cep_0123456789abcdef01234567",
                        },
                        consent_lease_factory=lambda *_: nullcontext({}),
                        health_sender=lambda _endpoint, event: (
                            health_events.append(event) or "accepted"
                        ),
                    )
            skipped = telemetry_async.run_once(
                self.home,
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
                consent_lease_factory=lambda *_: nullcontext({}),
                health_sender=lambda _endpoint, event: (
                    health_events.append(event) or "accepted"
                ),
            )

        self.assertEqual(telemetry_async.CIRCUIT_FAILURE_THRESHOLD, worker.call_count)
        self.assertEqual(1, skipped["circuit_open"])
        self.assertEqual(1, len(health_events))
        self.assertEqual(
            "helixharness-telemetry-runtime-health/2.0",
            health_events[0]["schema"],
        )
        self.assertEqual("circuit_open", health_events[0]["event"])
        self.assertNotIn("repo", health_events[0])
        state = telemetry_async.runtime_health_status(self.home)
        self.assertTrue(state["circuit_open"])
        self.assertEqual(
            telemetry_async.CIRCUIT_FAILURE_THRESHOLD,
            state["consecutive_failures"],
        )

    def test_collection_circuit_is_scoped_to_repository_platform(self) -> None:
        failing_repo = self.home / "failing-repo"
        healthy_repo = self.home / "healthy-repo"
        for repo in (failing_repo, healthy_repo):
            input_root = repo / ".helixharness" / "telemetry-v2" / "inputs"
            input_root.mkdir(parents=True)
            (input_root / "entry-0123456789abcdef.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            telemetry_async.register_repository(repo, "codex", self.home)

        calls = {"failing": 0, "healthy": 0}

        def collect(repo: Path, *, home: Path, platform: str | None = None):
            self.assertEqual(self.home.resolve(), home)
            self.assertEqual("codex", platform)
            if repo == failing_repo.resolve():
                calls["failing"] += 1
                return {"collected": 0, "skipped": 0, "failed": 1}
            calls["healthy"] += 1
            return {"collected": 1, "skipped": 0, "failed": 0}

        with mock.patch.object(
            telemetry_collector,
            "collect_finished",
            side_effect=collect,
        ):
            for _ in range(telemetry_async.CIRCUIT_FAILURE_THRESHOLD + 1):
                report = telemetry_async.run_once(
                    self.home,
                    consent_resolver=lambda *_: {
                        "effective_mode": "enabled",
                        "consent_epoch": "cep_0123456789abcdef01234567",
                    },
                )

        self.assertEqual(telemetry_async.CIRCUIT_FAILURE_THRESHOLD, calls["failing"])
        self.assertEqual(telemetry_async.CIRCUIT_FAILURE_THRESHOLD + 1, calls["healthy"])
        self.assertEqual(1, report["collection_circuits_open"])
        health = telemetry_async.runtime_health_status(self.home)
        self.assertFalse(health["circuit_open"])
        scoped = list(health["collection_circuits"].values())
        self.assertEqual(1, len(scoped))
        self.assertTrue(scoped[0]["circuit_open"])
        self.assertEqual("codex", scoped[0]["platform"])

    def test_disabled_project_does_not_collect_or_retain_failure_state(self) -> None:
        repo = self.home / "disabled-repo"
        input_root = repo / ".helixharness" / "telemetry-v2" / "inputs"
        input_root.mkdir(parents=True)
        (input_root / "entry-0123456789abcdef.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        telemetry_async.register_repository(repo, "codex", self.home)
        with mock.patch.object(
            telemetry_collector,
            "collect_finished",
            return_value={"collected": 0, "skipped": 0, "failed": 1},
        ):
            telemetry_async.run_once(
                self.home,
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
            )

        self.assertEqual(
            1,
            len(telemetry_async.runtime_health_status(self.home)["collection_circuits"]),
        )
        with mock.patch.object(
            telemetry_collector,
            "collect_finished",
            side_effect=AssertionError("disabled project must not be collected"),
        ):
            report = telemetry_async.run_once(
                self.home,
                consent_resolver=lambda *_: {
                    "effective_mode": "disabled",
                    "consent_epoch": None,
                },
            )

        self.assertEqual(0, report["collection_failed"])
        self.assertEqual("telemetry_disabled", report["repository_results"][0]["reason_code"])
        self.assertEqual(
            {},
            telemetry_async.runtime_health_status(self.home)["collection_circuits"],
        )

    def test_delivery_circuit_does_not_block_healthy_collection(self) -> None:
        repo = self.home / "healthy-while-delivery-down"
        input_root = repo / ".helixharness" / "telemetry-v2" / "inputs"
        input_root.mkdir(parents=True)
        (input_root / "entry-0123456789abcdef.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        telemetry_async.register_repository(repo, "codex", self.home)
        collection_calls = 0
        send_calls = 0

        def collect(_repo: Path, *, home: Path, platform: str | None = None):
            nonlocal collection_calls
            self.assertEqual(self.home.resolve(), home)
            self.assertEqual("codex", platform)
            collection_calls += 1
            return {"collected": 1, "skipped": 0, "failed": 0}

        def sender(_endpoint: str, _items: list[dict]):
            nonlocal send_calls
            send_calls += 1
            raise OSError("endpoint unavailable")

        with mock.patch.object(
            telemetry_collector,
            "collect_finished",
            side_effect=collect,
        ):
            for index in range(telemetry_async.CIRCUIT_FAILURE_THRESHOLD + 1):
                item = _supplement(f"supp_{index + 100:016x}")
                telemetry_async.Outbox(self.home).enqueue(
                    item,
                    repo=repo,
                    platform="codex",
                    consent_epoch="cep_0123456789abcdef01234567",
                    endpoint="http://example.test/report",
                )
                report = telemetry_async.run_once(
                    self.home,
                    sender=sender,
                    consent_resolver=lambda *_: {
                        "effective_mode": "enabled",
                        "consent_epoch": "cep_0123456789abcdef01234567",
                    },
                    consent_lease_factory=lambda *_: nullcontext({}),
                )

        self.assertEqual(telemetry_async.CIRCUIT_FAILURE_THRESHOLD + 1, collection_calls)
        self.assertEqual(telemetry_async.CIRCUIT_FAILURE_THRESHOLD, send_calls)
        self.assertEqual(1, report["delivery_circuits_open"])
        health = telemetry_async.runtime_health_status(self.home)
        self.assertFalse(health["circuit_open"])
        scoped = list(health["delivery_circuits"].values())
        self.assertEqual(1, len(scoped))
        self.assertTrue(scoped[0]["circuit_open"])

    def test_runtime_health_send_holds_the_selected_consent_epoch_lease(self) -> None:
        repo = self.home / "health-consent-repo"
        repo.mkdir()
        telemetry_async.register_repository(repo, "codex", self.home)
        state = telemetry_async._new_runtime_state()
        state.update(
            {
                "consecutive_failures": telemetry_async.CIRCUIT_FAILURE_THRESHOLD,
                "circuit_open": True,
                "circuit_resume_at": telemetry_async.time.time() + 3600,
            }
        )
        telemetry_async._save_runtime_state(self.home, state)
        order: list[str] = []

        @contextmanager
        def lease(leased_repo: Path, platform: str, epoch: str):
            self.assertEqual(repo.resolve(), leased_repo.resolve())
            self.assertEqual("codex", platform)
            self.assertEqual("cep_0123456789abcdef01234567", epoch)
            order.append("lease-enter")
            try:
                yield {}
            finally:
                order.append("lease-exit")

        telemetry_async.run_once(
            self.home,
            consent_resolver=lambda *_: {
                "effective_mode": "enabled",
                "consent_epoch": "cep_0123456789abcdef01234567",
            },
            consent_lease_factory=lease,
            health_sender=lambda *_: order.append("send") or "accepted",
        )

        self.assertEqual(["lease-enter", "send", "lease-exit"], order)

    def test_runtime_health_is_not_reported_without_enabled_consent(self) -> None:
        with mock.patch.object(
            telemetry_async,
            "_run_once_unlocked",
            side_effect=OSError("runtime unavailable"),
        ), mock.patch.object(telemetry_async, "_http_health_sender") as health_sender:
            for _ in range(telemetry_async.CIRCUIT_FAILURE_THRESHOLD):
                with self.assertRaises(OSError):
                    telemetry_async.run_once(self.home, health_sender=health_sender)

        self.assertTrue(telemetry_async.runtime_health_status(self.home)["circuit_open"])
        health_sender.assert_not_called()

    def test_successful_empty_run_resets_prior_failure_counter(self) -> None:
        failure = {
            "pending": 1,
            "accepted": 0,
            "duplicate": 0,
            "retryable": 1,
            "rejected": 0,
            "consent_removed": 0,
            "remaining": 1,
            "collection_failed": 0,
            "quarantined": 0,
            "skipped_existing_worker": 0,
        }
        success = {
            **failure,
            "pending": 0,
            "retryable": 0,
            "remaining": 0,
        }
        with mock.patch.object(
            telemetry_async,
            "_run_once_unlocked",
            side_effect=[OSError("runtime unavailable"), success],
        ):
            with self.assertRaises(OSError):
                telemetry_async.run_once(self.home)
            telemetry_async.run_once(self.home)

        state = telemetry_async.runtime_health_status(self.home)
        self.assertEqual(0, state["consecutive_failures"])
        self.assertFalse(state["circuit_open"])

    def test_abandoned_sixth_worker_opens_circuit_before_new_work(self) -> None:
        state = telemetry_async._new_runtime_state()
        state.update(
            {
                "consecutive_failures": telemetry_async.CIRCUIT_FAILURE_THRESHOLD - 1,
                "active_run": {
                    "run_id": "run_abandoned",
                    "pid": 123,
                    "started_at": "2026-08-14T00:00:00Z",
                },
            }
        )
        telemetry_async._save_runtime_state(self.home, state)

        with mock.patch.object(telemetry_async, "_run_once_unlocked") as worker:
            result = telemetry_async.run_once(self.home)

        self.assertEqual(1, result["circuit_open"])
        worker.assert_not_called()

    def test_open_circuit_suppresses_stage_finish_dispatch(self) -> None:
        state = telemetry_async._new_runtime_state()
        state.update(
            {
                "circuit_open": True,
                "circuit_resume_at": telemetry_async.time.time() + 3600,
            }
        )
        telemetry_async._save_runtime_state(self.home, state)

        with mock.patch.object(telemetry_async.subprocess, "Popen") as popen:
            self.assertFalse(telemetry_async.wake_detached(self.home))

        popen.assert_not_called()

    def test_operator_repair_clears_open_circuit_after_scheduler_is_healthy(self) -> None:
        state = telemetry_async._new_runtime_state()
        state.update(
            {
                "consecutive_failures": telemetry_async.CIRCUIT_FAILURE_THRESHOLD,
                "circuit_open": True,
                "circuit_resume_at": telemetry_async.time.time() + 3600,
            }
        )
        telemetry_async._save_runtime_state(self.home, state)
        with mock.patch.object(
            telemetry_async, "install_runtime"
        ), mock.patch.object(
            telemetry_async, "ensure_scheduler", return_value={"healthy": True}
        ), mock.patch.object(
            telemetryctl.sys,
            "argv",
            ["telemetryctl", "--home", str(self.home), "repair"],
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(0, telemetryctl.main())

        health = telemetry_async.runtime_health_status(self.home)
        self.assertFalse(health["circuit_open"])
        self.assertEqual(0, health["consecutive_failures"])

    def test_windows_stage_finish_wakes_managed_task_without_waiting_for_worker(self) -> None:
        with mock.patch.object(
            telemetry_async.system_platform, "system", return_value="Windows"
        ), mock.patch.object(
            telemetry_async.subprocess, "Popen"
        ) as popen:
            self.assertTrue(telemetry_async.wake_detached(self.home))

        self.assertEqual(
            ["schtasks", "/Run", "/TN", telemetry_async.WINDOWS_TASK_NAME],
            popen.call_args.args[0],
        )
        self.assertIs(telemetry_async.subprocess.DEVNULL, popen.call_args.kwargs["stdout"])


    def test_runtime_repair_installs_stable_cli_and_registers_repo_without_scheduler_in_test_mode(self) -> None:
        repo = self.home / "repair-repo"
        repo.mkdir()
        plugin_root = Path(__file__).resolve().parent.parent.parent
        with mock.patch.dict(os.environ, {"HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1"}):
            repaired = telemetry_async.ensure_runtime_ready(plugin_root, repo, "codex", self.home)

        self.assertEqual("disabled-by-environment", repaired["scheduler"])
        self.assertTrue((self.home / "bin" / "telemetryctl").is_file())
        self.assertTrue((self.home / "runtime-manifest.json").is_file())
        self.assertEqual(str(repo.resolve()), telemetry_async.registered_repositories(self.home)[0]["path"])

    def test_runtime_install_includes_ledger_import_dependencies(self) -> None:
        plugin_root = Path(__file__).resolve().parent.parent.parent
        telemetry_async.install_runtime(plugin_root, self.home)
        runtime = self.home / "runtime"

        self.assertTrue((runtime / "git_target.py").is_file())
        completed = telemetry_async.subprocess.run(
            [
                telemetry_async.sys.executable,
                "-c",
                (
                    "import git_target, telemetry_async, telemetry_collector, "
                    "telemetry_consent_state, telemetry_ledger, "
                    "telemetry_protocol, telemetryctl"
                ),
            ],
            cwd=runtime,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_runtime_repair_refreshes_changed_files_without_version_bump(self) -> None:
        repo = self.home / "refresh-repo"
        repo.mkdir()
        plugin_root = Path(__file__).resolve().parent.parent.parent
        telemetry_async.install_runtime(plugin_root, self.home)
        installed = self.home / "runtime" / "telemetry_async.py"
        installed.write_text("stale runtime\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1"}):
            telemetry_async.ensure_runtime_ready(
                plugin_root, repo, "codex", self.home
            )

        self.assertEqual(
            (plugin_root / "scripts" / "telemetry_async.py").read_bytes(),
            installed.read_bytes(),
        )

    def test_repository_registration_preserves_all_telemetry_hosts(self) -> None:
        repo = self.home / "handoff-repo"
        repo.mkdir()

        telemetry_async.register_repository(repo, "codex", self.home)
        telemetry_async.register_repository(repo, "claude-code", self.home)

        registered = telemetry_async.registered_repositories(self.home)
        self.assertEqual(1, len(registered))
        self.assertEqual(["claude-code", "codex"], registered[0]["platforms"])
        self.assertNotIn("platform", registered[0])

    def test_concurrent_repository_registration_does_not_drop_repositories(self) -> None:
        repositories = [self.home / f"repo-{index}" for index in range(12)]
        for repo in repositories:
            repo.mkdir()
        start = threading.Barrier(len(repositories))
        failures: list[BaseException] = []

        def register(repo: Path) -> None:
            try:
                start.wait()
                telemetry_async.register_repository(repo, "codex", self.home)
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=register, args=(repo,)) for repo in repositories]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        self.assertEqual(
            sorted(str(repo.resolve()) for repo in repositories),
            [item["path"] for item in telemetry_async.registered_repositories(self.home)],
        )

    def test_telemetryctl_commands_return_failure_when_recovery_is_incomplete(self) -> None:
        cases = (
            (
                "status",
                ["status"],
                (
                    mock.patch.object(telemetry_async, "scheduler_status", return_value={"healthy": False}),
                ),
            ),
            (
                "run",
                ["run"],
                (
                    mock.patch.object(
                        telemetry_async,
                        "run_once",
                        return_value={
                            "collection_failed": 1,
                            "remaining": 0,
                            "rejected": 0,
                            "quarantined": 0,
                        },
                    ),
                ),
            ),
            (
                "backfill",
                ["backfill", "--all"],
                (
                    mock.patch.object(telemetry_async, "registered_repositories", return_value=[]),
                    mock.patch.object(
                        telemetry_async,
                        "run_once",
                        return_value={
                            "collection_failed": 0,
                            "remaining": 1,
                            "rejected": 0,
                            "quarantined": 0,
                        },
                    ),
                ),
            ),
            (
                "repair",
                ["repair"],
                (
                    mock.patch.object(telemetry_async, "install_runtime"),
                    mock.patch.object(telemetry_async, "ensure_scheduler", return_value={"healthy": False}),
                ),
            ),
        )
        for name, arguments, patches in cases:
            with self.subTest(command=name), mock.patch.object(
                telemetryctl.sys,
                "argv",
                ["telemetryctl", "--home", str(self.home / name), *arguments],
            ), redirect_stdout(io.StringIO()):
                for active_patch in patches:
                    active_patch.start()
                    self.addCleanup(active_patch.stop)
                self.assertEqual(1, telemetryctl.main())
                for active_patch in reversed(patches):
                    active_patch.stop()

    def test_manual_backfill_filters_registered_repo_and_stage_end_time(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        old_item = _supplement("supp_4444444444444444")
        new_item = _supplement("supp_5555555555555555")
        outbox.enqueue(old_item, repo=Path("/tmp/old-repo"), platform="codex", consent_epoch="cep_0123456789abcdef01234567", source_ended_at="2026-08-01T00:00:00Z")
        outbox.enqueue(new_item, repo=Path("/tmp/new-repo"), platform="codex", consent_epoch="cep_0123456789abcdef01234567", source_ended_at="2026-08-12T00:00:00Z")
        sent: list[str] = []

        def sender(_endpoint: str, items: list[dict]) -> list[dict]:
            sent.extend(item["supplement_id"] for item in items)
            return [{"supplement_id": item["supplement_id"], "status": "accepted"} for item in items]

        telemetry_async.run_once(
            self.home,
            sender=sender,
            consent_resolver=lambda *_: {"effective_mode": "enabled", "consent_epoch": "cep_0123456789abcdef01234567"},
            repo_filter={Path("/tmp/new-repo").resolve()},
            source_since="2026-08-10T00:00:00Z",
        )
        self.assertEqual([new_item["supplement_id"]], sent)
        self.assertEqual([old_item["supplement_id"]], [item["supplement_id"] for item in outbox.pending()])

    def test_consent_epoch_change_deletes_unsent_private_evidence(self) -> None:
        outbox = telemetry_async.Outbox(self.home)
        item = _supplement("supp_3333333333333333")
        outbox.enqueue(
            item,
            repo=Path("/tmp/repo"),
            platform="claude-code",
            consent_epoch="cep_0123456789abcdef01234567",
            endpoint="http://example.test/report",
        )
        called = False

        def sender(_endpoint: str, _items: list[dict]) -> list[dict]:
            nonlocal called
            called = True
            return []

        report = telemetry_async.run_once(
            self.home,
            sender=sender,
            consent_resolver=lambda *_: {"effective_mode": "disabled", "consent_epoch": None},
        )
        self.assertFalse(called)
        self.assertEqual(1, report["consent_removed"])
        self.assertEqual([], outbox.pending())

    def test_collector_requires_enabled_exact_consent_epoch_before_reading_log(self) -> None:
        repo = self.home / "repo-consent"
        repo.mkdir()
        session = self.home / "never-read.jsonl"
        session.write_text("{}\n", encoding="utf-8")
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Collector Tester"], check=True)
        started = telemetry_ledger.start_stage(
            repo,
            feature="consent-boundary",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-12T01:00:00Z",
        )
        telemetry_ledger.finish_stage(repo, started["entry_id"], status="completed", ended_at="2026-08-12T01:01:00Z")

        with mock.patch.object(telemetry_collector.telemetry_consent, "consent_lease", side_effect=telemetry_collector.telemetry_consent.ConsentLeaseUnavailable("disabled")):
            with self.assertRaises(telemetry_collector.telemetry_consent.ConsentLeaseUnavailable):
                telemetry_collector.collect_entry(repo, started["entry_id"], home=self.home)
        self.assertEqual([], telemetry_async.Outbox(self.home).pending())

    def test_collector_reads_only_exact_window_and_enqueues_sanitized_supplement(self) -> None:
        repo = self.home / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Collector Tester"], check=True)
        session = self.home / "codex-home" / "sessions" / "2026" / "08" / "12" / "rollout.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(
                [
                    '{"timestamp":"2026-08-12T00:59:59Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"output_tokens":2}},"model":"gpt-test"}}',
                    '{"timestamp":"2026-08-12T01:00:01Z","type":"response_item","payload":{"type":"user_message","message":"修复 token=secret-123"}}',
                    '{"timestamp":"2026-08-12T01:00:01.500Z","type":"response_item","payload":{"type":"agent_message","message":"正在读取上下文。"}}',
                    '{"timestamp":"2026-08-12T01:00:02Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20,"output_tokens":5}},"model":"gpt-test"}}',
                    '{"timestamp":"2026-08-12T01:00:03Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"call-1","arguments":"cat /private/source.py"}}',
                    '{"timestamp":"2026-08-12T01:00:04Z","type":"response_item","payload":{"type":"function_call_output","call_id":"call-1","output":"exit_code=0 source"}}',
                    '{"timestamp":"2026-08-12T01:00:04.100Z","type":"response_item","payload":{"type":"function_call","name":"read_file","call_id":"call-2","arguments":"{\\"path\\":\\"docs/knowledge/index.md\\"}"}}',
                    '{"timestamp":"2026-08-12T01:00:04.200Z","type":"response_item","payload":{"type":"function_call_output","call_id":"call-2","output":"knowledge"}}',
                    '{"timestamp":"2026-08-12T01:00:04.300Z","type":"response_item","payload":{"type":"function_call","name":"spawn_agent","call_id":"call-3","arguments":"{\\"task\\":\\"review\\"}"}}',
                    '{"timestamp":"2026-08-12T01:00:04.400Z","type":"response_item","payload":{"type":"function_call_output","call_id":"call-3","output":"agent-1"}}',
                    '{"timestamp":"2026-08-12T01:00:05Z","type":"response_item","payload":{"type":"agent_message","message":"已完成并验证。"}}',
                    '{"timestamp":"2026-08-12T02:00:00Z","type":"response_item","payload":{"type":"user_message","message":"outside"}}',
                    '{"timestamp":"2026-08-12T02:00:01Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":999,"output_tokens":999}},"model":"outside-model"}}',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        started = telemetry_ledger.start_stage(
            repo,
            feature="checkout-timeout",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-12T01:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            started["entry_id"],
            status="completed",
            ended_at="2026-08-12T01:01:00Z",
        )
        enabled_lease = mock.MagicMock()
        enabled_lease.__enter__.return_value = {"effective_mode": "enabled"}
        enabled_lease.__exit__.return_value = False
        lease_factory = mock.Mock(return_value=enabled_lease)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            first_report = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 1, "skipped": 0, "failed": 0}, first_report)
        first_supplement = telemetry_async.Outbox(self.home).pending()[0]
        self.assertEqual("unavailable", first_supplement["workflow_trace"]["status"])

        telemetry_trace.start_trace(
            repo,
            entry_id=started["entry_id"],
            command="apply",
            route_reason="variant_resolved",
            plugin_root=Path(__file__).resolve().parent.parent.parent,
            helixharness_version="2.2.0",
        )
        telemetry_trace.record_event(
            repo,
            started["entry_id"],
            event_id="archive_preflight",
            kind="gate",
            status="passed",
        )
        telemetry_trace.finish_trace(
            repo,
            started["entry_id"],
            status="completed",
            failure_code=None,
            quality={"verification": "passed"},
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            second_report = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 1, "skipped": 0, "failed": 0}, second_report)
        supplements = telemetry_async.Outbox(self.home).pending()
        self.assertEqual([1, 2], sorted(item["revision"] for item in supplements))
        supplement = next(item for item in supplements if item["revision"] == 2)
        self.assertEqual(first_supplement["supplement_id"], supplement["supersedes"])
        self.assertEqual(10, supplement["usage"]["input_tokens"])
        self.assertEqual(3, supplement["usage"]["output_tokens"])
        self.assertEqual("gpt-test", supplement["usage"]["model"])
        self.assertEqual(
            {
                "platform": "codex",
                "session_id": "session-raw",
                "session_hash": supplement["session_hash"],
                "session_kind": "primary",
            },
            supplement["session"],
        )
        self.assertEqual(3, supplement["evidence"]["tools"]["succeeded"])
        self.assertEqual(3, supplement["execution"]["tool_call_count"])
        self.assertEqual(1, supplement["execution"]["subagent_spawn_count"])
        self.assertEqual(1, supplement["execution"]["context_reads"]["knowledge"])
        self.assertEqual(
            "variant_resolved",
            supplement["workflow_trace"]["route"]["decision_code"],
        )
        self.assertEqual(
            "archive_preflight",
            supplement["workflow_trace"]["gates"][0]["check_id"],
        )
        self.assertEqual(
            {"duration_ms", "name", "status"},
            set(supplement["execution"]["tools"][0]),
        )
        self.assertEqual("exec_command", supplement["execution"]["tools"][0]["name"])
        self.assertGreaterEqual(supplement["execution"]["tools"][0]["duration_ms"], 0)
        self.assertIn("[REDACTED_SECRET]", str(supplement["evidence"]))
        self.assertNotIn("outside", str(supplement))
        self.assertIn("正在读取上下文", str(supplement))
        self.assertEqual(
            ["正在读取上下文。", "已完成并验证。"],
            supplement["evidence"]["assistant_replies"],
        )
        self.assertEqual(2, len(telemetry_async.Outbox(self.home).pending()))

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            unchanged = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 0, "skipped": 1, "failed": 0}, unchanged)
        self.assertEqual(3, lease_factory.call_count)

        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-08-12T01:00:06Z","type":"response_item",'
                '"payload":{"type":"agent_message","message":"补充验证完成。"}}\n'
            )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            revised_report = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 1, "skipped": 0, "failed": 0}, revised_report)
        supplements = telemetry_async.Outbox(self.home).pending()
        self.assertEqual(3, len(supplements))
        revised = next(item for item in supplements if item["revision"] == 3)
        self.assertEqual(supplement["supplement_id"], revised["supersedes"])
        self.assertIn("补充验证完成。", revised["evidence"]["assistant_replies"])
        self.assertEqual(4, lease_factory.call_count)

    def test_collector_uses_frozen_input_after_ledger_leaves_worktree(self) -> None:
        repo = self.home / "repo-branch-switch"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Collector Tester"],
            check=True,
        )
        session = (
            self.home
            / "codex-home"
            / "sessions"
            / "2026"
            / "08"
            / "24"
            / "branch-switch.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text(
            '{"timestamp":"2026-08-24T01:00:01Z","type":"response_item",'
            '"payload":{"type":"agent_message","message":"完成验证"}}\n',
            encoding="utf-8",
        )
        started = telemetry_ledger.start_stage(
            repo,
            feature="branch-switch-safe",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-24T01:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            started["entry_id"],
            status="completed",
            ended_at="2026-08-24T01:01:00Z",
        )

        telemetry_async.register_repository(repo, "codex", self.home)
        telemetry_ledger.ledger_path(repo, "branch-switch-safe").unlink()
        enabled_lease = mock.MagicMock()
        enabled_lease.__enter__.return_value = {"effective_mode": "enabled"}
        enabled_lease.__exit__.return_value = False
        sent: list[dict] = []

        def sender(_endpoint: str, items: list[dict]) -> list[dict]:
            sent.extend(items)
            return [
                {"supplement_id": item["supplement_id"], "status": "accepted"}
                for item in items
            ]

        with mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.home / "codex-home")},
        ), mock.patch.object(
            telemetry_collector.telemetry_consent,
            "consent_lease",
            return_value=enabled_lease,
        ):
            report = telemetry_async.run_once(
                self.home,
                sender=sender,
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
                consent_lease_factory=lambda *_: nullcontext({}),
            )

        self.assertEqual(0, report["collection_failed"])
        self.assertEqual(1, report["accepted"])
        self.assertEqual(1, len(sent))
        supplement = sent[0]
        self.assertEqual(started["entry_id"], supplement["entry_id"])
        self.assertEqual("branch-switch-safe", supplement["feature"])
        private_root = repo / ".helixharness" / "telemetry-v2"
        self.assertTrue(
            (private_root / "locators" / f'{started["entry_id"]}.json').is_file()
        )
        self.assertTrue(
            (private_root / "inputs" / f'{started["entry_id"]}.json').is_file()
        )
        marker = private_root / "stages" / f'{started["entry_id"]}.collected'
        self.assertEqual(
            "collected",
            json.loads(marker.read_text(encoding="utf-8"))["state"],
        )

    def test_final_ack_cleans_delivered_entry_private_state(self) -> None:
        repo = self.home / "repo-delivered"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Collector Tester"],
            check=True,
        )
        session = (
            self.home
            / "codex-home"
            / "sessions"
            / "2026"
            / "08"
            / "24"
            / "delivered.jsonl"
        )
        session.parent.mkdir(parents=True)
        session.write_text(
            '{"timestamp":"2026-08-24T02:00:01Z","type":"response_item",'
            '"payload":{"type":"agent_message","message":"归档完成"}}\n',
            encoding="utf-8",
        )
        archived = telemetry_ledger.start_stage(
            repo,
            feature="delivered-cleanup",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-24T02:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            archived["entry_id"],
            status="completed",
            ended_at="2026-08-24T02:01:00Z",
        )
        private_root = repo / ".helixharness" / "telemetry-v2"
        trace = private_root / "traces" / f'{archived["entry_id"]}.json'
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("{}\n", encoding="utf-8")
        telemetry_async.register_repository(repo, "codex", self.home)
        enabled_lease = mock.MagicMock()
        enabled_lease.__enter__.return_value = {"effective_mode": "enabled"}
        enabled_lease.__exit__.return_value = False

        with mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.home / "codex-home")},
        ), mock.patch.object(
            telemetry_collector.telemetry_consent,
            "consent_lease",
            return_value=enabled_lease,
        ):
            report = telemetry_async.run_once(
                self.home,
                sender=lambda _endpoint, items: [
                    {"supplement_id": item["supplement_id"], "status": "accepted"}
                    for item in items
                ],
                consent_resolver=lambda *_: {
                    "effective_mode": "enabled",
                    "consent_epoch": "cep_0123456789abcdef01234567",
                },
                consent_lease_factory=lambda *_: nullcontext({}),
            )

        self.assertEqual(1, report["accepted"])
        self.assertTrue(
            telemetry_ledger.ledger_path(repo, "delivered-cleanup").is_file()
        )
        entry_id = archived["entry_id"]
        for path in (
            private_root / "locators" / f"{entry_id}.json",
            private_root / "traces" / f"{entry_id}.json",
            private_root / "stages" / f"{entry_id}.json",
            private_root / "stages" / f"{entry_id}.collected",
            private_root / "inputs" / f"{entry_id}.json",
            private_root / "seals" / "delivered-cleanup" / "collection.json",
        ):
            self.assertFalse(path.exists(), str(path))

    def test_missing_legacy_collection_input_is_quarantined_once(self) -> None:
        repo = self.home / "repo-unrecoverable"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Collector Tester"],
            check=True,
        )
        started = telemetry_ledger.start_stage(
            repo,
            feature="lost-ledger",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(self.home / "missing-session.jsonl"),
            started_at="2026-08-24T03:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            started["entry_id"],
            status="completed",
            ended_at="2026-08-24T03:01:00Z",
        )
        entry_id = started["entry_id"]
        (
            repo
            / ".helixharness"
            / "telemetry-v2"
            / "inputs"
            / f"{entry_id}.json"
        ).unlink()
        telemetry_ledger.ledger_path(repo, "lost-ledger").unlink()

        first = telemetry_collector.collect_finished(repo, home=self.home)

        self.assertEqual({"collected": 0, "skipped": 1, "failed": 0}, first)
        marker = (
            repo
            / ".helixharness"
            / "telemetry-v2"
            / "stages"
            / f"{entry_id}.quarantined"
        )
        quarantine = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual("quarantined", quarantine["state"])
        self.assertEqual("fixed_ledger_unavailable", quarantine["reason_code"])

        with mock.patch.object(
            telemetry_collector,
            "_collect_entry",
            side_effect=AssertionError("quarantined entry must not be retried"),
        ):
            second = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 0, "skipped": 1, "failed": 0}, second)

    def test_archive_seal_excludes_later_log_changes_from_the_requirement(self) -> None:
        repo = self.home / "repo-sealed"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Collector Tester"], check=True)
        session = self.home / "codex-home" / "sessions" / "2026" / "08" / "12" / "sealed.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            '{"timestamp":"2026-08-12T01:00:01Z","type":"response_item",'
            '"payload":{"type":"agent_message","message":"归档前内容"}}\n',
            encoding="utf-8",
        )
        apply = telemetry_ledger.start_stage(
            repo,
            feature="sealed-content",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-12T01:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            apply["entry_id"],
            status="completed",
            ended_at="2026-08-12T01:01:00Z",
        )
        enabled_lease = mock.MagicMock()
        enabled_lease.__enter__.return_value = {"effective_mode": "enabled"}
        enabled_lease.__exit__.return_value = False
        lease_factory = mock.Mock(return_value=enabled_lease)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            telemetry_collector.collect_finished(repo, home=self.home)

        archive = telemetry_ledger.start_stage(
            repo,
            feature="sealed-content",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-raw",
            session_log_path=str(session),
            started_at="2026-08-12T02:00:00Z",
        )
        telemetry_ledger.finish_stage(
            repo,
            archive["entry_id"],
            status="completed",
            ended_at="2026-08-12T02:01:00Z",
        )
        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-08-12T01:00:02Z","type":"response_item",'
                '"payload":{"type":"agent_message","message":"归档后、Worker 前补写内容"}}\n'
            )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex-home")}), mock.patch.object(
            telemetry_collector.telemetry_consent, "consent_lease", new=lease_factory
        ):
            final_snapshot = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 1, "skipped": 1, "failed": 0}, final_snapshot)
        for entry_id in (apply["entry_id"], archive["entry_id"]):
            marker = (
                repo
                / ".helixharness"
                / "telemetry-v2"
                / "stages"
                / f"{entry_id}.collected"
            )
            self.assertEqual(
                "sealed_collected",
                json.loads(marker.read_text(encoding="utf-8"))["state"],
            )
        frozen_count = len(telemetry_async.Outbox(self.home).pending())
        self.assertNotIn(
            "归档后、Worker 前补写内容",
            json.dumps(telemetry_async.Outbox(self.home).pending(), ensure_ascii=False),
        )

        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-08-12T01:00:03Z","type":"response_item",'
                '"payload":{"type":"agent_message","message":"归档后补写内容"}}\n'
            )
        with mock.patch.object(
            telemetry_collector,
            "_collect_entry",
            side_effect=AssertionError("sealed entry must not be scanned again"),
        ):
            after_archive = telemetry_collector.collect_finished(repo, home=self.home)

        self.assertEqual({"collected": 0, "skipped": 2, "failed": 0}, after_archive)
        supplements = telemetry_async.Outbox(self.home).pending()
        self.assertEqual(frozen_count, len(supplements))
        self.assertNotIn("归档后补写内容", json.dumps(supplements, ensure_ascii=False))

        session.unlink()
        with mock.patch.object(
            telemetry_collector,
            "_collect_entry",
            side_effect=AssertionError("sealed entry must survive source rotation"),
        ):
            after_rotation = telemetry_collector.collect_finished(repo, home=self.home)
        self.assertEqual({"collected": 0, "skipped": 2, "failed": 0}, after_rotation)


if __name__ == "__main__":
    unittest.main()
