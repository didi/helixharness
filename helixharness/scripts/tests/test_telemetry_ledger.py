from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import telemetry_ledger as telemetry_ledger
import eval_schema


class TelemetryLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Telemetry Tester"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "telemetry@example.test"],
            check=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_and_finish_write_only_fixed_evidence(self) -> None:
        evidence = self.repo / "docs/specs/active/checkout-timeout/verify-report.md"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("evidence\n", encoding="utf-8")
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="checkout-timeout",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-secret",
            session_log_path="/private/session.jsonl",
            started_at="2026-08-12T01:00:00.000Z",
        )
        finished = telemetry_ledger.finish_stage(
            self.repo,
            started["entry_id"],
            status="completed",
            ended_at="2026-08-12T01:02:00.000Z",
            quality={"standards_verdict": "APPROVE", "spec_verdict": "APPROVE"},
        )

        ledger = json.loads(Path(finished["path"]).read_text(encoding="utf-8"))
        self.assertEqual("helixharness-eval-ledger/1.0", ledger["schema"])
        self.assertEqual("1.0", ledger["schema_version"])
        self.assertRegex(ledger["requirement"]["id"], r"^req_[0-9a-f]{24}$")
        self.assertEqual("checkout-timeout", ledger["requirement"]["name"])
        self.assertNotIn("url", ledger["requirement"])
        self.assertEqual(self.repo.name, ledger["project"]["name"])
        self.assertNotIn("remote", str(ledger["project"]).lower())
        entry = ledger["entries"][0]
        self.assertNotIn("workflow_variant", entry)
        self.assertEqual("completed", entry["status"])
        self.assertEqual("APPROVE", entry["quality"]["standards_verdict"])
        self.assertEqual(
            ["docs/specs/active/checkout-timeout/verify-report.md"],
            entry["artifacts"],
        )
        self.assertNotIn("logical_stage_run_id", entry)
        self.assertNotIn("phase", entry)
        self.assertNotIn("resource_usage", entry)
        self.assertNotIn("session_window", entry)
        self.assertNotIn("telemetry_status", entry)
        self.assertRegex(entry["session_hash"], r"^session-[0-9a-f]{32}$")
        schema = json.loads(
            (Path(__file__).resolve().parent.parent.parent / "reference" / "eval-ledger-schema-v1.json").read_text(
                encoding="utf-8"
            )
        )
        eval_schema.validate(ledger, schema)

        locator = telemetry_ledger.load_locator(self.repo, started["entry_id"])
        self.assertEqual("session-secret", locator["raw_session_id"])
        self.assertEqual("/private/session.jsonl", locator["session_log_path"])
        self.assertNotIn("logical_stage_run_id", locator)
        if os.name != "nt":
            locator_path = self.repo / ".helixharness" / "telemetry-v2" / "locators" / f"{started['entry_id']}.json"
            self.assertEqual(0o600, locator_path.stat().st_mode & 0o777)


    def test_address_is_not_emitted_and_historical_value_is_not_rewritten(self) -> None:
        ledger = telemetry_ledger.ensure_ledger(self.repo, "demo")
        self.assertNotIn("url", ledger["requirement"])
        path = telemetry_ledger.ledger_path(self.repo, "demo")
        ledger["requirement"]["url"] = "historical-value-not-used-by-current-workflow"
        path.write_text(json.dumps(ledger), encoding="utf-8")
        started = telemetry_ledger.start_stage(
            self.repo, feature="demo", command="propose",
            platform="codex", trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
        )
        self.assertNotIn("requirement_url", started)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["requirement"]["id"], ledger["requirement"]["id"])
        self.assertEqual(stored["requirement"]["url"], ledger["requirement"]["url"])

    def test_stage_artifacts_are_derived_from_verified_workflow_outputs(self) -> None:
        cases = (
            (
                "artifact-propose",
                "propose",
                "active",
                ("spec.md", "tasks.md"),
            ),
            (
                "artifact-apply",
                "apply",
                "active",
                ("verify-report.md",),
            ),
            (
                "artifact-archive",
                "archive",
                "completed",
                ("spec.md", "tasks.md", "verify-report.md"),
            ),
        )
        for feature, command, area, names in cases:
            with self.subTest(command=command):
                root = self.repo / "docs/specs" / area / feature
                root.mkdir(parents=True)
                for name in names:
                    (root / name).write_text(name + "\n", encoding="utf-8")
                started = telemetry_ledger.start_stage(
                    self.repo,
                    feature=feature,
                    command=command,
                    platform="codex",
                    trigger="user_command",
                    consent_epoch="cep_0123456789abcdef01234567",
                )
                finished = telemetry_ledger.finish_stage(
                    self.repo,
                    started["entry_id"],
                    status="completed",
                )
                entry = json.loads(Path(finished["path"]).read_text(encoding="utf-8"))["entries"][0]
                self.assertEqual(
                    [f"docs/specs/{area}/{feature}/{name}" for name in names],
                    entry["artifacts"],
                )


    def test_earlier_schema_two_entry_fields_remain_readable(self) -> None:
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="compatible-lite-ledger",
            command="propose",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            started_at="2026-08-12T01:00:00.000Z",
        )
        path = Path(started["path"])
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["entries"][0]["logical_stage_run_id"] = "stage-run-0123456789abcdef"
        ledger["entries"][0]["phase"] = "legacy-phase"
        path.write_text(json.dumps(ledger), encoding="utf-8")

        compatible = telemetry_ledger.ensure_ledger(self.repo, "compatible-lite-ledger")
        schema = json.loads(
            (Path(__file__).resolve().parent.parent.parent / "reference/eval-ledger-schema-v1.json").read_text(
                encoding="utf-8"
            )
        )
        eval_schema.validate(compatible, schema)

    def test_requirement_accumulates_distinct_primary_sessions_by_entry(self) -> None:
        first = telemetry_ledger.start_stage(
            self.repo,
            feature="multi-session",
            command="propose",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-one",
            started_at="2026-08-12T01:00:00.000Z",
        )
        telemetry_ledger.finish_stage(
            self.repo,
            first["entry_id"],
            status="completed",
            ended_at="2026-08-12T01:01:00.000Z",
        )
        second = telemetry_ledger.start_stage(
            self.repo,
            feature="multi-session",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-two",
            started_at="2026-08-12T02:00:00.000Z",
        )

        ledger = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
        self.assertEqual(2, len(ledger["entries"]))
        self.assertEqual(2, len({entry["session_hash"] for entry in ledger["entries"]}))
        self.assertEqual("session-one", telemetry_ledger.load_locator(self.repo, first["entry_id"])["raw_session_id"])
        self.assertEqual("session-two", telemetry_ledger.load_locator(self.repo, second["entry_id"])["raw_session_id"])

    def test_completed_archive_freezes_registered_entries_and_rejects_new_sessions(self) -> None:
        first_session = self.repo / "session-one.jsonl"
        first_session.write_text('{"event":"first"}\n', encoding="utf-8")
        first = telemetry_ledger.start_stage(
            self.repo,
            feature="sealed-requirement",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-one",
            session_log_path=str(first_session),
            started_at="2026-08-12T01:00:00.000Z",
        )
        telemetry_ledger.finish_stage(
            self.repo,
            first["entry_id"],
            status="completed",
            ended_at="2026-08-12T01:01:00.000Z",
        )
        archive_session = self.repo / "session-two.jsonl"
        archive_session.write_text('{"event":"archive"}\n', encoding="utf-8")
        archive = telemetry_ledger.start_stage(
            self.repo,
            feature="sealed-requirement",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-two",
            session_log_path=str(archive_session),
            started_at="2026-08-12T02:00:00.000Z",
        )

        telemetry_ledger.finish_stage(
            self.repo,
            archive["entry_id"],
            status="completed",
            ended_at="2026-08-12T02:01:00.000Z",
        )

        seal = telemetry_ledger.load_collection_seal(self.repo, "sealed-requirement")
        self.assertEqual("helixharness-lite-collection-seal/1.0", seal["schema"])
        self.assertEqual(archive["entry_id"], seal["archive_entry_id"])
        self.assertEqual(
            [first["entry_id"], archive["entry_id"]],
            [entry["entry_id"] for entry in seal["entries"]],
        )
        self.assertEqual(
            [first_session.stat().st_size, archive_session.stat().st_size],
            [entry["source_limit_bytes"] for entry in seal["entries"]],
        )
        self.assertNotIn("session-one", json.dumps(seal))
        self.assertNotIn(str(first_session), json.dumps(seal))

        with self.assertRaisesRegex(telemetry_ledger.LedgerError, "archived.*sealed"):
            telemetry_ledger.start_stage(
                self.repo,
                feature="sealed-requirement",
                command="apply",
                platform="codex",
                trigger="user_command",
                consent_epoch="cep_0123456789abcdef01234567",
                raw_session_id="session-after-archive",
                session_log_path=str(first_session),
            )

    def test_completed_archive_rejects_late_finish_from_an_existing_session(self) -> None:
        apply = telemetry_ledger.start_stage(
            self.repo,
            feature="sealed-late-finish",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="apply-session",
        )
        archive = telemetry_ledger.start_stage(
            self.repo,
            feature="sealed-late-finish",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="archive-session",
        )
        telemetry_ledger.finish_stage(
            self.repo,
            archive["entry_id"],
            status="completed",
            ended_at="2026-08-12T02:01:00.000Z",
        )
        before = telemetry_ledger.ledger_path(
            self.repo, "sealed-late-finish"
        ).read_bytes()

        with self.assertRaisesRegex(telemetry_ledger.LedgerError, "archived.*sealed"):
            telemetry_ledger.finish_stage(
                self.repo,
                apply["entry_id"],
                status="completed",
                ended_at="2026-08-12T02:02:00.000Z",
            )

        self.assertEqual(
            before,
            telemetry_ledger.ledger_path(self.repo, "sealed-late-finish").read_bytes(),
        )

    def test_archive_seal_is_durable_before_public_archived_status(self) -> None:
        session = self.repo / "archive.jsonl"
        session.write_text('{"event":"archive"}\n', encoding="utf-8")
        archive = telemetry_ledger.start_stage(
            self.repo,
            feature="seal-first",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="archive-session",
            session_log_path=str(session),
        )
        original = telemetry_ledger._atomic_write
        failed_once = False

        def fail_first_seal(path: Path, value: dict) -> None:
            nonlocal failed_once
            if "seals" in path.parts and not failed_once:
                failed_once = True
                raise OSError("seal disk unavailable")
            original(path, value)

        with mock.patch.object(
            telemetry_ledger, "_atomic_write", side_effect=fail_first_seal
        ), self.assertRaisesRegex(OSError, "seal disk unavailable"):
            telemetry_ledger.finish_stage(
                self.repo,
                archive["entry_id"],
                status="completed",
                ended_at="2026-08-12T02:01:00.000Z",
            )

        ledger = json.loads(
            telemetry_ledger.ledger_path(self.repo, "seal-first").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("in_progress", ledger["status"])
        self.assertEqual("in_progress", ledger["entries"][0]["status"])

        telemetry_ledger.finish_stage(
            self.repo,
            archive["entry_id"],
            status="completed",
            ended_at="2026-08-12T02:01:00.000Z",
        )
        seal = telemetry_ledger.load_collection_seal(self.repo, "seal-first")
        self.assertEqual(archive["entry_id"], seal["archive_entry_id"])
        archived = json.loads(
            telemetry_ledger.ledger_path(self.repo, "seal-first").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("archived", archived["status"])

    def test_checkpoint_prefers_clean_commit_and_keeps_branch_fallback(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "master"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "switch", "-qc", "feature/checkpoint"], check=True)
        (self.repo / "README.md").write_text("feature\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "feature"], check=True)
        expected_head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        expected_base = subprocess.run(
            ["git", "-C", str(self.repo), "merge-base", "master", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        started = telemetry_ledger.start_stage(
            self.repo,
            feature="checkpoint",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-code-reference",
        )
        finished = telemetry_ledger.finish_stage(self.repo, started["entry_id"], status="completed")
        entry = json.loads(Path(finished["path"]).read_text(encoding="utf-8"))["entries"][0]
        checkpoint = entry["checkpoint"]
        self.assertEqual(expected_head, checkpoint["implementation_commit_id"])
        self.assertEqual("commit", checkpoint["code_resolution"])
        self.assertFalse(checkpoint["working_tree_dirty"])
        self.assertEqual(expected_head, checkpoint["base_commit"])
        self.assertEqual(expected_head, checkpoint["head_commit"])
        for branch_only in ("source_branch", "target_branch", "merge_base_commit"):
            self.assertNotIn(branch_only, checkpoint)

        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
        dirty = telemetry_ledger.start_stage(
            self.repo,
            feature="checkpoint-dirty",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-dirty",
        )
        dirty_finished = telemetry_ledger.finish_stage(self.repo, dirty["entry_id"], status="completed")
        dirty_checkpoint = json.loads(Path(dirty_finished["path"]).read_text(encoding="utf-8"))["entries"][0]["checkpoint"]
        self.assertEqual("branch", dirty_checkpoint["code_resolution"])
        self.assertNotIn("implementation_commit_id", dirty_checkpoint)
        self.assertEqual("feature/checkpoint", dirty_checkpoint["source_branch"])
        self.assertEqual("master", dirty_checkpoint["target_branch"])
        self.assertEqual(expected_base, dirty_checkpoint["merge_base_commit"])
        self.assertTrue(dirty_checkpoint["working_tree_dirty"])

    def test_clean_checkpoint_omits_branch_fallback_for_main_target(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "switch", "-qc", "feature/main-base"], check=True)
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="main-target",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-main-target",
        )

        finished = telemetry_ledger.finish_stage(self.repo, started["entry_id"], status="completed")
        checkpoint = json.loads(Path(finished["path"]).read_text(encoding="utf-8"))["entries"][0]["checkpoint"]

        self.assertEqual("commit", checkpoint["code_resolution"])
        self.assertNotIn("target_branch", checkpoint)
        self.assertNotIn("merge_base_commit", checkpoint)

    def test_dirty_master_never_claims_branch_relative_code_resolution(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "master"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="dirty-master",
            command="archive",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-dirty-master",
        )
        (self.repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        telemetry_ledger.finish_stage(self.repo, started["entry_id"], status="completed")

        ledger = json.loads(telemetry_ledger.ledger_path(self.repo, "dirty-master").read_text(encoding="utf-8"))
        checkpoint = ledger["entries"][0]["checkpoint"]
        self.assertEqual("unavailable", checkpoint["code_resolution"])
        self.assertNotIn("implementation_commit_id", checkpoint)

    def test_gitignore_change_counts_as_business_worktree_dirtiness(self) -> None:
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="gitignore-change",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-gitignore",
        )
        (self.repo / ".gitignore").write_text("generated/\n", encoding="utf-8")

        telemetry_ledger.finish_stage(self.repo, started["entry_id"], status="completed")

        ledger = json.loads(telemetry_ledger.ledger_path(self.repo, "gitignore-change").read_text(encoding="utf-8"))
        checkpoint = ledger["entries"][0]["checkpoint"]
        self.assertTrue(checkpoint["working_tree_dirty"])
        self.assertEqual("unavailable", checkpoint["code_resolution"])

    def test_existing_runtime_gitignore_rules_do_not_make_runtime_additions_dirty(self) -> None:
        (self.repo / ".gitignore").write_text("!docs/eval/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base ignore"], check=True)
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="runtime-ignore",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
            raw_session_id="session-runtime-ignore",
        )

        telemetry_ledger.finish_stage(self.repo, started["entry_id"], status="completed")

        ledger = json.loads(telemetry_ledger.ledger_path(self.repo, "runtime-ignore").read_text(encoding="utf-8"))
        checkpoint = ledger["entries"][0]["checkpoint"]
        self.assertFalse(checkpoint["working_tree_dirty"])
        self.assertEqual("commit", checkpoint["code_resolution"])

    def test_previous_ledger_version_is_not_rewritten(self) -> None:
        path = self.repo / "docs/eval/previous/eval-log.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"schema": "helixharness-eval-ledger/2.0", "schema_version": "2.0",
                               "feature": "previous", "entries": []}).encode()
        path.write_bytes(original)
        with self.assertRaises(telemetry_ledger.LedgerError):
            telemetry_ledger.ensure_ledger(self.repo, "previous")
        self.assertEqual(original, path.read_bytes())

    def test_schema_one_legacy_log_is_never_migrated_or_rewritten(self) -> None:
        path = self.repo / "docs" / "eval" / "legacy-feature" / "eval-log.json"
        path.parent.mkdir(parents=True)
        original = b'{"schema_version":"1.0","feature":"legacy-feature","entries":[]}\n'
        path.write_bytes(original)

        with self.assertRaisesRegex(telemetry_ledger.LedgerError, "unsupported Lite eval-log schema"):
            telemetry_ledger.start_stage(
                self.repo,
                feature="legacy-feature",
                command="apply",
                platform="claude-code",
                trigger="user_command",
                consent_epoch="cep_0123456789abcdef01234567",
            )

        self.assertEqual(original, path.read_bytes())
        self.assertFalse((self.repo / ".helixharness" / "telemetry-v2" / "locators").exists())

    def test_new_entry_uses_canonical_actor_identity_fields(self) -> None:
        started = telemetry_ledger.start_stage(
            self.repo,
            feature="actor-contract",
            command="propose",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
        )
        ledger = json.loads(Path(started["path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "user_name": "Telemetry Tester",
                "user_name_source": "git_config_local_user_name",
            },
            ledger["entries"][0]["actor"],
        )

    def test_rejects_nonportable_feature_and_unregistered_host_but_allows_handoff(self) -> None:
        for invalid in ("bad.", "COM1.txt", ".git"):
            with self.subTest(invalid=invalid), self.assertRaises(telemetry_ledger.LedgerError):
                telemetry_ledger.start_stage(
                    self.repo,
                    feature=invalid,
                    command="apply",
                    platform="codex",
                    trigger="user_command",
                    consent_epoch="cep_0123456789abcdef01234567",
                )
        with self.assertRaises(telemetry_ledger.LedgerError):
            telemetry_ledger.start_stage(
                self.repo,
                feature="unsupported-host",
                command="apply",
                platform="cursor",
                trigger="user_command",
                consent_epoch="cep_0123456789abcdef01234567",
            )
        first = telemetry_ledger.start_stage(
            self.repo,
            feature="host-handoff",
            command="apply",
            platform="codex",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
        )
        second = telemetry_ledger.start_stage(
            self.repo,
            feature="host-handoff",
            command="archive",
            platform="claude-code",
            trigger="user_command",
            consent_epoch="cep_0123456789abcdef01234567",
        )
        ledger = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(first["entry_id"], second["entry_id"])
        self.assertEqual(["codex", "claude-code"], [entry["platform"] for entry in ledger["entries"]])

    def test_lock_times_out_instead_of_waiting_forever(self) -> None:
        with mock.patch.object(telemetry_ledger, "LOCK_TIMEOUT_SECONDS", 0.02), mock.patch.object(
            telemetry_ledger.fcntl, "flock", side_effect=BlockingIOError()
        ), mock.patch.object(time, "sleep", return_value=None):
            with self.assertRaisesRegex(telemetry_ledger.LedgerError, "timed out"):
                with telemetry_ledger._lock(self.repo):
                    pass

    def test_public_write_survives_private_locator_failure(self) -> None:
        original = telemetry_ledger._atomic_write

        def fail_private(path: Path, value: dict) -> None:
            if ".helixharness" in path.parts:
                raise OSError("private disk unavailable")
            original(path, value)

        with mock.patch.object(telemetry_ledger, "_atomic_write", side_effect=fail_private):
            started = telemetry_ledger.start_stage(
                self.repo,
                feature="public-first",
                command="apply",
                platform="codex",
                trigger="user_command",
                consent_epoch="cep_0123456789abcdef01234567",
            )
        self.assertEqual("unavailable", started["session_locator_status"])
        self.assertTrue((self.repo / "docs" / "eval" / "public-first" / "eval-log.json").is_file())


if __name__ == "__main__":
    unittest.main()
