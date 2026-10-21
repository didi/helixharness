#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_entry_load import (
    MAX_RESPONSE_BYTES,
    EntryLoadError,
    finish_entry,
    prepare_entry,
    read_entry_page,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ENTRY_FIXTURE_ROOTS = ("commands", "hosts", "reference", "workflows")


class CodexEntryLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, command: str, resources: tuple[str, ...] = ()) -> dict:
        return prepare_entry(
            PLUGIN_ROOT,
            command=command,
            resources=resources,
            state_root=self.state_root,
        )

    def test_prepare_is_compact_and_loads_one_projected_command(self) -> None:
        packet = self.prepare("apply")
        paths = [item["path"] for item in packet["packets"][0]["resources"]]

        self.assertEqual("codex", packet["host_id"])
        self.assertEqual("codex", packet["canonical_host_id"])
        self.assertNotIn("bundle_digest", packet)
        self.assertEqual(2, packet["max_rounds"])
        self.assertEqual(1, len(packet["packets"]))
        self.assertNotIn("content", json.dumps(packet))
        self.assertLess(
            len(json.dumps(packet, ensure_ascii=False).encode("utf-8")),
            MAX_RESPONSE_BYTES,
        )
        self.assertIn("commands/apply.md", paths)
        self.assertNotIn("commands/propose.md", paths)
        self.assertIn("hosts/codex/runtime.md", paths)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(
            all(item["source_sha256"] for item in packet["packets"][0]["resources"])
        )

    def test_adapter_packet_loads_only_requested_resources_and_shared_protocol(self) -> None:
        packet = self.prepare("apply", ("subagent_protocol",))
        paths = [item["path"] for item in packet["packets"][1]["resources"]]

        self.assertEqual(
            [
                "reference/subagent-runtime-protocol.md",
                "reference/codex-subagent-runtime-protocol.md",
                "hosts/shared/subagent-result-encoding.md",
            ],
            paths,
        )
        self.assertNotIn("commands/apply.md", paths)

    def test_unknown_adapter_is_rejected(self) -> None:
        with self.assertRaisesRegex(EntryLoadError, "not allowed"):
            self.prepare("apply", ("telemetry",))

    def test_command_cannot_request_an_unrelated_adapter(self) -> None:
        with self.assertRaisesRegex(EntryLoadError, "not allowed"):
            self.prepare("update", ("ut",))


    def test_projection_has_exactly_one_command_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            for included in ENTRY_FIXTURE_ROOTS:
                shutil.copytree(PLUGIN_ROOT / included, root / included)
            projection_path = root / "hosts/codex/command-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            projection["entrypoints"]["apply"].append("commands/propose.md")
            projection_path.write_text(json.dumps(projection), encoding="utf-8")
            with self.assertRaisesRegex(EntryLoadError, "ambiguous"):
                prepare_entry(
                    root,
                    command="apply",
                    state_root=self.state_root,
                )

    def test_unsupported_host_api_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            for included in ENTRY_FIXTURE_ROOTS:
                shutil.copytree(PLUGIN_ROOT / included, root / included)
            host_path = root / "hosts/codex/host.json"
            host = json.loads(host_path.read_text(encoding="utf-8"))
            host["host_api_version"] = "2.0"
            host_path.write_text(json.dumps(host), encoding="utf-8")

            with self.assertRaisesRegex(EntryLoadError, "Host Context"):
                prepare_entry(
                    root,
                    command="apply",
                    state_root=self.state_root,
                )

    def test_large_utf8_resource_is_losslessly_paged_below_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            for included in ENTRY_FIXTURE_ROOTS:
                shutil.copytree(PLUGIN_ROOT / included, root / included)
            command_path = root / "commands/apply.md"
            large_suffix = ("界\\\"page-safe\n" * 12000)
            command_path.write_text(
                command_path.read_text(encoding="utf-8") + large_suffix,
                encoding="utf-8",
            )
            expected = command_path.read_text(encoding="utf-8")
            self.assertGreater(len(expected.encode("utf-8")), 100 * 1024)

            prepared = prepare_entry(
                root,
                command="apply",
                state_root=Path(temporary) / "state",
            )
            reconstructed = ""
            for packet in prepared["packets"]:
                for page_index in range(packet["page_count"]):
                    page = read_entry_page(
                        root,
                        invocation_key=prepared["invocation_key"],
                        packet_round=packet["round"],
                        page_index=page_index,
                        state_root=Path(temporary) / "state",
                    )
                    self.assertLess(
                        len(json.dumps(page, ensure_ascii=False).encode("utf-8")),
                        MAX_RESPONSE_BYTES,
                    )
                    if page["path"] == "commands/apply.md":
                        reconstructed += page["content"]
            self.assertEqual(expected, reconstructed)
            finish_entry(
                root,
                invocation_key=prepared["invocation_key"],
                state_root=Path(temporary) / "state",
            )

    def test_source_changes_leave_prepared_pages_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            state_root = Path(temporary) / "state"
            for included in ENTRY_FIXTURE_ROOTS:
                shutil.copytree(PLUGIN_ROOT / included, root / included)
            command_path = root / "commands/apply.md"
            original = command_path.read_text(encoding="utf-8") + "\nfirst-revision\n"
            command_path.write_text(original, encoding="utf-8")

            prepared = prepare_entry(root, command="apply", state_root=state_root)
            command_path.write_text("second-revision\n", encoding="utf-8")

            reconstructed = ""
            for packet in prepared["packets"]:
                for page_index in range(packet["page_count"]):
                    page = read_entry_page(
                        root,
                        invocation_key=prepared["invocation_key"],
                        packet_round=packet["round"],
                        page_index=page_index,
                        state_root=state_root,
                    )
                    if page["path"] == "commands/apply.md":
                        reconstructed += page["content"]
            self.assertEqual(original, reconstructed)

    def test_every_codex_skill_requests_only_its_command_allowlist(self) -> None:
        skills_root = PLUGIN_ROOT / "hosts/codex/skills"
        projection = json.loads(
            (PLUGIN_ROOT / "hosts/codex/command-projection.json").read_text(
                encoding="utf-8"
            )
        )
        for command in sorted(projection["entrypoints"]):
            entry = skills_root / command / "SKILL.md"
            text = entry.read_text(encoding="utf-8")
            resources = tuple(dict.fromkeys(re.findall(r"--resource ([a-z_]+)", text)))
            with self.subTest(command=command):
                packet = self.prepare(command, resources)
                self.assertLessEqual(len(packet["packets"]), 2)
                for logical_packet in packet["packets"]:
                    for page_index in range(logical_packet["page_count"]):
                        page = read_entry_page(
                            PLUGIN_ROOT,
                            invocation_key=packet["invocation_key"],
                            packet_round=logical_packet["round"],
                            page_index=page_index,
                            state_root=self.state_root,
                        )
                        self.assertLess(
                            len(json.dumps(page, ensure_ascii=False).encode("utf-8")),
                            MAX_RESPONSE_BYTES,
                        )
                finish_entry(
                    PLUGIN_ROOT,
                    invocation_key=packet["invocation_key"],
                    state_root=self.state_root,
                )

    def test_init_and_ut_entries_load_the_resources_the_workflow_consumes(self) -> None:
        required = {
            "init": {"project_config", "project_hooks", "subagent_protocol", "telemetry_protocol"},
            "ut": {"ut", "subagent_protocol", "telemetry_protocol"},
        }
        host = json.loads((PLUGIN_ROOT / "hosts/codex/host.json").read_text())
        for command, keys in required.items():
            with self.subTest(command=command):
                entry = (PLUGIN_ROOT / f"hosts/codex/skills/{command}/SKILL.md").read_text()
                resources = tuple(re.findall(r"--resource ([a-z_]+)", entry))
                prepared = self.prepare(command, resources)
                paths = {item["path"] for packet in prepared["packets"] for item in packet["resources"]}
                expected = {host["adapters"][key].removeprefix("resource:") for key in keys}
                self.assertLessEqual(expected, paths, f"{command} entry packet omits required resources")
                footer = (PLUGIN_ROOT / f"commands/{command}.md").read_text().split("宿主资源声明：")[-1]
                self.assertLessEqual(keys, set(re.findall(r"resources\.([a-z_]+)", footer)))

    def test_public_cli_prepare_page_finish_seam(self) -> None:
        loader = PLUGIN_ROOT / "scripts/codex_entry_load.py"
        base = [
            "python3",
            str(loader),
            "--plugin-root",
            str(PLUGIN_ROOT),
            "--state-root",
            str(self.state_root),
        ]
        prepared_process = subprocess.run(
            [*base, "--command", "update"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, prepared_process.returncode, prepared_process.stdout)
        prepared = json.loads(prepared_process.stdout)
        self.assertLess(len(prepared_process.stdout.encode("utf-8")), MAX_RESPONSE_BYTES)
        for packet in prepared["packets"]:
            for page_index in range(packet["page_count"]):
                page_process = subprocess.run(
                    [
                        *base,
                        "--read-page",
                        "--invocation-key",
                        prepared["invocation_key"],
                        "--packet-round",
                        str(packet["round"]),
                        "--page-index",
                        str(page_index),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, page_process.returncode, page_process.stdout)
                self.assertLess(
                    len(page_process.stdout.encode("utf-8")), MAX_RESPONSE_BYTES
                )
        finished_process = subprocess.run(
            [*base, "--finish", "--invocation-key", prepared["invocation_key"]],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("finished", json.loads(finished_process.stdout)["status"])

        rejected_process = subprocess.run(
            [*base, "--command", "unknown-command"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, rejected_process.returncode)
        self.assertEqual(
            "command projection is missing or ambiguous",
            json.loads(rejected_process.stdout)["reason"],
        )

    def test_pages_are_bounded_and_reconstruct_every_selected_resource(self) -> None:
        prepared = self.prepare(
            "propose",
            (
                "subagent_protocol",
                "telemetry_protocol",
            ),
        )
        reconstructed: dict[str, str] = {}
        for packet in prepared["packets"]:
            for page_index in range(packet["page_count"]):
                page = read_entry_page(
                    PLUGIN_ROOT,
                    invocation_key=prepared["invocation_key"],
                    packet_round=packet["round"],
                    page_index=page_index,
                    state_root=self.state_root,
                )
                encoded = json.dumps(page, ensure_ascii=False).encode("utf-8")
                self.assertLess(len(encoded), MAX_RESPONSE_BYTES)
                self.assertEqual(
                    hashlib.sha256(page["content"].encode("utf-8")).hexdigest(),
                    page["page_sha256"],
                )
                reconstructed.setdefault(page["path"], "")
                reconstructed[page["path"]] += page["content"]

        expected_paths = {
            item["path"]
            for packet in prepared["packets"]
            for item in packet["resources"]
        }
        self.assertEqual(expected_paths, set(reconstructed))
        for relative, content in reconstructed.items():
            self.assertEqual(
                (PLUGIN_ROOT / relative).read_text(encoding="utf-8"),
                content,
            )

    def test_page_reads_are_idempotent_and_finish_invalidates_state(self) -> None:
        prepared = self.prepare("apply")
        arguments = {
            "invocation_key": prepared["invocation_key"],
            "packet_round": 1,
            "page_index": 0,
            "state_root": self.state_root,
        }
        first = read_entry_page(PLUGIN_ROOT, **arguments)
        self.assertEqual(first, read_entry_page(PLUGIN_ROOT, **arguments))

        for packet in prepared["packets"]:
            start = 1 if packet["round"] == 1 else 0
            for page_index in range(start, packet["page_count"]):
                read_entry_page(
                    PLUGIN_ROOT,
                    invocation_key=prepared["invocation_key"],
                    packet_round=packet["round"],
                    page_index=page_index,
                    state_root=self.state_root,
                )

        finished = finish_entry(
            PLUGIN_ROOT,
            invocation_key=prepared["invocation_key"],
            state_root=self.state_root,
        )
        self.assertEqual("finished", finished["status"])
        with self.assertRaisesRegex(EntryLoadError, "missing or expired"):
            read_entry_page(PLUGIN_ROOT, **arguments)

    def test_out_of_order_page_and_early_finish_fail_closed(self) -> None:
        prepared = self.prepare("apply", ("subagent_protocol",))
        with self.assertRaisesRegex(EntryLoadError, "out of order"):
            read_entry_page(
                PLUGIN_ROOT,
                invocation_key=prepared["invocation_key"],
                packet_round=1,
                page_index=1,
                state_root=self.state_root,
            )
        with self.assertRaisesRegex(EntryLoadError, "not fully consumed"):
            finish_entry(
                PLUGIN_ROOT,
                invocation_key=prepared["invocation_key"],
                state_root=self.state_root,
            )

    def test_invocation_key_cannot_escape_state_root(self) -> None:
        with self.assertRaisesRegex(EntryLoadError, "invalid invocation key"):
            read_entry_page(
                PLUGIN_ROOT,
                invocation_key="../outside",
                packet_round=1,
                page_index=0,
                state_root=self.state_root,
            )

    def test_nonempty_unowned_state_root_is_preserved(self) -> None:
        unowned = Path(self.temporary.name) / "unowned"
        unowned.mkdir()
        business = unowned / "business.txt"
        business.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(EntryLoadError, "not owned"):
            prepare_entry(
                PLUGIN_ROOT,
                command="update",
                state_root=unowned,
            )
        self.assertEqual("keep", business.read_text(encoding="utf-8"))

    def test_expired_state_and_concurrent_prepare_keys_fail_safe(self) -> None:
        first = self.prepare("update")
        second = self.prepare("update")
        self.assertNotEqual(first["invocation_key"], second["invocation_key"])
        manifest_path = self.state_root / first["invocation_key"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["expires_at"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(EntryLoadError, "missing or expired"):
            read_entry_page(
                PLUGIN_ROOT,
                invocation_key=first["invocation_key"],
                packet_round=1,
                page_index=0,
                state_root=self.state_root,
            )

    def test_concurrent_prepare_does_not_delete_inflight_snapshots(self) -> None:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self.prepare, "update") for _ in range(20)]
            prepared = [future.result() for future in futures]
        self.assertEqual(20, len({item["invocation_key"] for item in prepared}))
        self.assertTrue(
            all(
                (self.state_root / item["invocation_key"] / "manifest.json").is_file()
                for item in prepared
            )
        )

    def test_tampered_page_fails_closed(self) -> None:
        prepared = self.prepare("apply")
        invocation = self.state_root / prepared["invocation_key"]
        page_path = next(invocation.glob("page-*.txt"))
        page_path.chmod(0o600)
        page_path.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(EntryLoadError, "page identity"):
            read_entry_page(
                PLUGIN_ROOT,
                invocation_key=prepared["invocation_key"],
                packet_round=1,
                page_index=0,
                state_root=self.state_root,
            )


if __name__ == "__main__":
    unittest.main()
