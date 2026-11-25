"""Explicit OpenCode D2C checks against synthetic filesystem evidence only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone

import env_sandbox


PLUGIN = Path(__file__).resolve().parents[2]
GATE = PLUGIN / "hosts/opencode/d2c_gate.cjs"


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class OpenCodeD2CGateTests(unittest.TestCase):
    @staticmethod
    def png(width=1, height=1, raw=None, compressed=None):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        if raw is None:
            raw = (b"\0" + b"\xff\xff\xff\xff" * width) * height
        if compressed is None:
            compressed = zlib.compress(raw)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "synthetic project"
        self.page = "synthetic-page"
        self.page_dir = self.root / "d-d2c/temp" / self.page
        self.round_dir = self.page_dir / "r1"
        self.round_dir.mkdir(parents=True)
        self.started = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        self.state_file = self.page_dir / "workflow-state.json"
        self.marker_file = self.page_dir.parent / ".active-workflow.json"
        self.state = {"currentPhase": 0, "round": 1, "phases": {
            "0": {"status": "in_progress", "enteredAt": self.started},
        }}
        self.marker = {"pageDir": self.page, "workflowStartedAt": self.started,
                       "stateFile": str(self.state_file), "status": "active"}
        self.save()

    def save(self):
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")
        self.marker_file.write_text(json.dumps(self.marker), encoding="utf-8")

    def run_gate(self, action="stop", session="synthetic-native-session", *extra):
        args = [shutil.which("node"), str(GATE), "--repo-root", str(self.root),
                "--page-dir", self.page, "--started-at", self.started, "--action", action]
        if session is not None:
            args += ["--session-id", session]
        result = subprocess.run(args + list(extra), cwd=self.root, capture_output=True,
                                text=True, timeout=10, env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.stderr, "", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def owned(self):
        self.marker["ownerSessionId"] = "synthetic-native-session"
        self.marker["ownerClaimedAt"] = self.started
        self.save()

    def completed(self, visual=True, current=6):
        self.owned()
        self.state["phases"] = {str(n): {"status": "completed", "enteredAt": self.started,
                                         "completedAt": self.started} for n in (0, 1, 2, 3, 4, 6)}
        self.state["phases"]["4"]["decision"] = "cancel"
        self.state["currentPhase"] = current
        if current == 4:
            self.state["phases"]["4"] = {"status": "in_progress", "enteredAt": self.started}
            del self.state["phases"]["6"]
        report = self.root / "d2c/temp/d2c-fidelity-evaluator-report/d2c-fidelity-evaluator-r1.md"
        self.state["phases"]["3"].update(report=str(report), score=94)
        self.save()
        if visual:
            report.parent.mkdir(parents=True, exist_ok=True)
            self.evaluation = {"version": "1.0", "round": 1, "reportFile": str(report), "overallScore": 94,
                               "scoreUnit": "percent", "summary": "Synthetic fixture evaluation; no real browser acceptance.",
                               "dimensions": {key: {"weight": weight, "score": 94, "deduction": 6, "notes": "Synthetic fixture"}
                                              for key, weight in (("layoutStructure", .4), ("visualStyle", .25),
                                                                  ("contentAssets", .15), ("perceptualSimilarity", .2))},
                               "optimizationPlan": [], "findings": [], "unresolvedQuestions": []}
            self.write_report()
            # A real tiny PNG fixture, not browser or visual acceptance evidence.
            actual = self.round_dir / "synthetic-actual.png"
            actual.write_bytes(self.png())
            self.meta = {"outPath": str(actual), "actualSha256": hashlib.sha256(actual.read_bytes()).hexdigest(),
                         "captureMode": "full-page", "materialStatus": "valid",
                         "runtimeHealth": {"status": "passed", "askUser": False},
                         "sourceFingerprint": {"digest": hashlib.sha256(b"d2c-runtime-source-v1\0").hexdigest(),
                                               "files": [], "explicitFiles": [], "excludedFiles": []}}
            self.write_meta()
            for name in ("geometry", "vertical", "render"):
                measured = {"wDesign": 1, "leftDesign": 0} if name == "geometry" else (
                    {"top": 0, "height": 1, "bottom": 1} if name == "vertical" else {
                        "matched": True, "layoutMark": "synthetic-scope",
                        "style": {"rectWidth": 1, "rectHeight": 1, "backgroundColor": "rgb(255,255,255)", "opacity": "1"}})
                measurement = {"version": "1.0", "pageDir": str(self.page_dir), "url": "http://localhost/synthetic",
                               "captureMode": "full-page", "designFrameWidth": 1, "effectiveCssWidth": 1,
                               "scaleCssToDesign": 1, "checkedCount": 1, "matchedCount": 1,
                               "items": [{"layoutMark": "synthetic-scope", "matched": True,
                                          "expected": {"w": 1, "absX": 0, "top": 0, "height": 1}, "measured": measured}],
                               "violations": [], "violationCount": 0}
                if name == "vertical":
                    measurement.update(tool="d2c-vertical-check", mode="page-flow", primaryCount=0, derivedCount=0)
                if name == "render":
                    measurement.update(gate="render-integrity", hardViolationCount=0)
                    measurement["items"][0]["expected"] = {"visualFill": "rgba(255,255,255,1)", "textSpans": [], "assets": [], "opacity": 1}
                (self.round_dir / f"{name}-diff.json").write_text(json.dumps(measurement), encoding="utf-8")

    def write_report(self):
        Path(self.state["phases"]["3"]["report"]).write_text(
            "# Synthetic fidelity report\n**Fidelity Score**: 94%\n```json\n" + json.dumps(self.evaluation) + "\n```\n", encoding="utf-8")

    def write_meta(self):
        (self.round_dir / "capture-meta.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def test_claim_binds_fresh_phase0_then_checks_are_read_only(self):
        code, result = self.run_gate("claim")
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "claimed")
        self.assertEqual(json.loads(self.marker_file.read_text())["ownerSessionId"], "synthetic-native-session")
        before = self.marker_file.read_bytes(), self.state_file.read_bytes()
        self.assertEqual(self.run_gate()[0], 2)
        self.assertEqual(before, (self.marker_file.read_bytes(), self.state_file.read_bytes()))

    def test_missing_session_and_unowned_completion_are_rejected(self):
        self.completed()
        self.assertEqual(self.run_gate(session=None)[0], 2)
        del self.marker["ownerSessionId"]
        self.save()
        self.assertEqual(self.run_gate()[0], 2)
        self.assertEqual(self.run_gate("claim")[0], 2)

    def test_other_session_cannot_claim_or_complete(self):
        self.completed()
        for action in ("claim", "stop", "ask"):
            with self.subTest(action=action):
                self.assertEqual(self.run_gate(action, "another-native-session")[0], 2)

    def test_stale_unowned_phase0_cannot_be_claimed(self):
        self.started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.state["phases"]["0"]["enteredAt"] = self.started
        self.marker["workflowStartedAt"] = self.started
        self.save()
        self.assertEqual(self.run_gate("claim")[0], 2)

    def test_claim_cannot_reclaim_owned_state(self):
        self.owned()
        self.assertEqual(self.run_gate("claim")[0], 2)

    def test_ownership_claim_must_belong_to_this_invocation(self):
        self.completed()
        for claimed in (None, "2000-01-01T00:00:00Z"):
            self.marker["ownerClaimedAt"] = claimed
            self.save()
            with self.subTest(claimed=claimed):
                self.assertEqual(self.run_gate()[0], 2)

    def test_complete_status_without_actual_evidence_is_rejected(self):
        self.completed(visual=False)
        code, result = self.run_gate()
        self.assertEqual(code, 2)
        self.assertFalse(result["allow"])

    def test_present_evidence_allows_completion_not_spurious_ask(self):
        self.completed()
        code, result = self.run_gate()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["action"]["kind"], "done")
        self.assertEqual(self.run_gate("ask")[0], 2)

    def test_missing_each_visual_artifact_is_rejected(self):
        self.completed()
        paths = [self.round_dir / name for name in ("capture-meta.json", "synthetic-actual.png",
                 "geometry-diff.json", "vertical-diff.json", "render-diff.json")]
        paths += [Path(self.state["phases"]["3"]["report"])]
        for file in paths:
            with self.subTest(file=file.name):
                saved = file.read_bytes()
                file.unlink()
                self.assertEqual(self.run_gate()[0], 2)
                file.write_bytes(saved)

    def test_stale_files_rejected_even_when_status_timestamps_are_current(self):
        self.completed()
        for file in (self.round_dir / "synthetic-actual.png", self.round_dir / "render-diff.json",
                     Path(self.state["phases"]["3"]["report"])):
            with self.subTest(file=file.name):
                stat = file.stat()
                os.utime(file, (1, 1))
                self.assertEqual(self.run_gate()[0], 2)
                os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_modified_capture_bytes_and_invalid_material_are_rejected(self):
        self.completed()
        actual = self.round_dir / "synthetic-actual.png"
        original = actual.read_bytes()
        actual.write_bytes(original + b"changed")
        self.assertEqual(self.run_gate()[0], 2)
        actual.write_bytes(original)
        self.meta["materialStatus"] = "invalid"
        self.write_meta()
        self.assertEqual(self.run_gate()[0], 2)

    def test_empty_measurements_and_malformed_json_are_rejected(self):
        self.completed()
        for content in ("{}", "[]", "invalid", ""):
            with self.subTest(content=content):
                (self.round_dir / "geometry-diff.json").write_text(content)
                self.assertEqual(self.run_gate()[0], 2)

    def test_phase4_needs_evidence_and_headless_cannot_ask(self):
        self.completed(current=4)
        self.assertEqual(self.run_gate("ask")[0], 0)
        self.assertEqual(self.run_gate("ask", "synthetic-native-session", "--headless")[0], 2)
        (self.round_dir / "synthetic-actual.png").unlink()
        self.assertEqual(self.run_gate("ask")[0], 2)

    def test_external_input_and_cancel_preserve_distinct_legal_outcomes(self):
        self.owned()
        for kind, expected in (("external-input", "wait-external-input"), ("user-cancelled", "aborted")):
            self.state["workflowPause"] = {"kind": kind, "reason": "synthetic", "detail": "Synthetic reason", "createdAt": self.started}
            self.save()
            code, result = self.run_gate()
            self.assertEqual(code, 0, result)
            self.assertEqual(result["action"]["kind"], expected)

    def test_file_and_directory_symlink_escapes_are_rejected(self):
        self.completed()
        evidence = self.round_dir / "render-diff.json"
        outside = Path(self.temp.name) / "outside.json"
        outside.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(outside)
        self.assertEqual(self.run_gate()[0], 2)

    def test_old_completed_phase6_cannot_mask_fresh_restart(self):
        self.completed()
        self.state["currentPhase"] = 0
        self.state["phases"]["0"]["status"] = "in_progress"
        self.save()
        self.assertEqual(self.run_gate()[0], 2)

    def test_missing_failed_and_stale_runtime_health_are_rejected(self):
        self.completed()
        healthy = self.meta["runtimeHealth"]
        for health in (None, {"status": "failed"}, {"status": "passed", "askUser": True}):
            self.meta["runtimeHealth"] = health
            self.write_meta()
            with self.subTest(health=health):
                self.assertEqual(self.run_gate()[0], 2)
        self.meta["runtimeHealth"] = healthy
        self.meta["sourceFingerprint"]["digest"] = "0" * 64
        self.write_meta()
        self.assertEqual(self.run_gate()[0], 2)

    def test_source_edit_after_capture_invalidates_runtime_evidence(self):
        self.completed()
        source = self.root / "synthetic.vue"
        source.write_text("<template>Synthetic</template>")
        digest = hashlib.sha256(b"d2c-runtime-source-v1\0synthetic.vue\0" + source.read_bytes() + b"\0").hexdigest()
        self.meta["sourceFingerprint"].update(digest=digest, files=["synthetic.vue"], explicitFiles=["synthetic.vue"])
        self.write_meta()
        self.assertEqual(self.run_gate()[0], 0)
        source.write_text("<template>Changed</template>")
        self.assertEqual(self.run_gate()[0], 2)

    def test_region_evidence_needs_design_scope_and_fresh_design_source(self):
        self.completed()
        self.meta.update(captureMode="region", designScope="synthetic-scope")
        self.write_meta()
        for name in ("geometry", "vertical", "render"):
            path = self.round_dir / f"{name}-diff.json"
            report = json.loads(path.read_text())
            report.update(captureMode="region", scope={"layoutMarks": ["synthetic-scope"]})
            path.write_text(json.dumps(report))
        self.assertEqual(self.run_gate()[0], 2)
        design = self.round_dir / "synthetic-design-source.png"
        design.write_bytes((self.round_dir / "synthetic-actual.png").read_bytes())
        self.assertEqual(self.run_gate()[0], 0)
        os.utime(design, (1, 1))
        self.assertEqual(self.run_gate()[0], 2)

    def test_stale_handoff_does_not_authorize_current_stop(self):
        self.owned()
        (self.page_dir / "browser-handoff.json").write_text(json.dumps({
            "active": True, "status": "awaiting-user", "updatedAt": "2000-01-01T00:00:00Z",
            "browserReadyAt": "2000-01-01T00:00:00Z"}))
        self.assertEqual(self.run_gate()[0], 2)

    def test_real_checkpoint_restart_claims_new_native_session(self):
        self.completed()
        checkpoint = PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"
        result = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.state = json.loads(self.state_file.read_text())
        self.marker = json.loads(self.marker_file.read_text())
        self.started = self.state["phases"]["0"]["enteredAt"]
        self.assertIsNone(self.marker["ownerSessionId"])
        self.assertIn("6", self.state["phases"])
        self.assertEqual(self.run_gate("claim", "second-native-session")[0], 0)
        self.assertEqual(self.run_gate("stop", "second-native-session")[0], 2)

    def test_unknown_duplicate_and_invalid_arguments_are_rejected(self):
        for action, extra in (("invalid", ()), ("stop", ("--unknown", "x")),
                              ("stop", ("--session-id", "duplicate"))):
            with self.subTest(action=action, extra=extra):
                self.assertEqual(self.run_gate(action, "synthetic-native-session", *extra)[0], 2)

    def test_git_discovered_source_symlink_rejected_before_external_read(self):
        self.completed()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        outside = Path(self.temp.name) / "outside-synthetic.vue"
        outside.write_bytes(b"synthetic outside repository")
        (self.root / "link.vue").symlink_to(outside)
        self.meta["sourceFingerprint"]["digest"] = hashlib.sha256(
            b"d2c-runtime-source-v1\0link.vue\0" + outside.read_bytes() + b"\0").hexdigest()
        self.write_meta()
        self.assertEqual(self.run_gate()[0], 2)
        # An exit code alone would not prove the external target was never read.
        observed = Path(self.temp.name) / "external-read-observed"
        preload = Path(self.temp.name) / "observe-read.cjs"
        preload.write_text("const fs=require('fs'); const read=fs.readFileSync; fs.readFileSync=function(file,...args){"
                           "try{if(fs.realpathSync(file)===process.env.TEST_OUTSIDE){fs.writeFileSync(process.env.TEST_OBSERVED,'read')}}catch{}"
                           "return read.call(fs,file,...args)}")
        result = subprocess.run([shutil.which("node"), "--require", str(preload), str(GATE),
                                 "--repo-root", str(self.root), "--page-dir", self.page, "--started-at", self.started,
                                 "--session-id", "synthetic-native-session", "--action", "stop"],
                                cwd=self.root, capture_output=True, text=True, timeout=10,
                                env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", ""), "TEST_OUTSIDE": str(outside), "TEST_OBSERVED": str(observed)})
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse(observed.exists(), "external source was read before the rejection")

    def test_invalid_png_structure_crc_deflate_and_scanline_are_rejected(self):
        self.completed()
        original = self.png()
        invalid_chunk_type = bytearray(original)
        invalid_chunk_type[12] |= 0x80
        invalid_chunk_type[29:33] = struct.pack(">I", zlib.crc32(invalid_chunk_type[12:29]))
        # Include a header-only fake and damaged IDAT/CRC, not just bad magic.
        candidates = [bytes.fromhex("89504e470d0a1a0a") + b"NOT_PNG!" + struct.pack(">II", 1, 1),
                      original[:-12], original[:45] + bytes([original[45] ^ 1]) + original[46:],
                      self.png(compressed=b"not-deflate"), self.png(raw=b"\x05\xff\xff\xff\xff"),
                      self.png(raw=b"\0\xff"), self.png(raw=b"\0\xff\xff\xff\xffextra"), invalid_chunk_type]
        for image in candidates:
            (self.round_dir / "synthetic-actual.png").write_bytes(image)
            self.meta["actualSha256"] = hashlib.sha256(image).hexdigest()
            self.write_meta()
            with self.subTest(length=len(image)):
                self.assertEqual(self.run_gate()[0], 2)

    def test_representative_long_png_valid_and_explicit_size_limit(self):
        self.completed()
        # DPR 2 captures of long pages must remain supported within the audit bound.
        image = self.png(width=1356, height=10000)
        (self.round_dir / "synthetic-actual.png").write_bytes(image)
        self.meta["actualSha256"] = hashlib.sha256(image).hexdigest()
        self.write_meta()
        self.assertEqual(self.run_gate()[0], 0)
        oversize = bytearray(self.png())
        oversize[16:20] = struct.pack(">I", 4000)
        oversize[20:24] = struct.pack(">I", 1000000)
        oversize[29:33] = struct.pack(">I", zlib.crc32(oversize[12:29]))
        (self.round_dir / "synthetic-actual.png").write_bytes(oversize)
        self.meta["actualSha256"] = hashlib.sha256(oversize).hexdigest()
        self.write_meta()
        code, result = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("audit limit", result["detail"])

    def test_zero_reports_and_positive_counts_without_measurements_are_rejected(self):
        self.completed()
        path = self.round_dir / "render-diff.json"
        original = json.loads(path.read_text())
        for change in ({"checkedCount": 0, "matchedCount": 0, "items": []},
                       {"items": [{"matched": True}]}, {"checkedCount": 2}):
            path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change):
                self.assertEqual(self.run_gate()[0], 2)

    def test_canonical_vertical_with_no_applicable_anchor_remains_legal(self):
        self.completed()
        path = self.round_dir / "vertical-diff.json"
        report = json.loads(path.read_text())
        report.update(checkedCount=0, matchedCount=0, items=[])
        path.write_text(json.dumps(report))
        self.assertEqual(self.run_gate()[0], 0)

    def test_canonical_vertical_producer_unresolved_warning_is_preserved(self):
        self.completed()
        # Execute the canonical producer unchanged. Only its browser boundary is
        # synthetic; DSL traversal and the emitted report are real producer code.
        (self.page_dir / "dsl_layout_marked.json").write_text(json.dumps({"nodes": [{
            "_layoutMark": "synthetic-scope", "type": "GROUP", "name": "Synthetic anchor",
            "layoutStyle": {"relativeX": 0, "relativeY": 0, "width": 1, "height": 1}, "children": []}]}))
        (self.page_dir / "layout-h5.json").write_text(json.dumps({
            "designFrameSize": {"width": 1}, "hostLayoutPolicy": "none"}))
        core = PLUGIN / "skills/d-d2c/scripts/lib/d2c-dom-measure-core.cjs"
        preload = Path(self.temp.name) / "synthetic-browser-boundary.cjs"
        preload.write_text("const core=require(" + json.dumps(str(core)) + ");"
                           "core.loadPuppeteer=()=>({chrome:null,puppeteer:{}});"
                           "core.openBrowserSession=async()=>({sessionId:'synthetic-browser',targetId:'synthetic-target',attached:false,"
                           "page:{evaluate:async(fn,payload)=>({viewportCssWidth:1,effectiveCssWidth:1,"
                           "results:payload.targets.map(target=>({layoutMark:target.layoutMark,matched:false}))})}});"
                           "core.closeBrowserSession=async()=>{}")
        producer = PLUGIN / "skills/d-d2c/scripts/d2c-vertical-check.cjs"
        result = subprocess.run([shutil.which("node"), "--require", str(preload), str(producer),
                                 "--url", "http://localhost/synthetic", "--page-dir", str(self.page_dir),
                                 "--out", str(self.round_dir), "--scope", "synthetic-scope", "--wait", "0"],
                                cwd=self.root, capture_output=True, text=True, timeout=10,
                                env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        path = self.round_dir / "vertical-diff.json"
        original = path.read_bytes()
        report = json.loads(original)
        self.assertEqual((report["checkedCount"], report["matchedCount"], report["violationCount"]), (1, 0, 0))
        self.assertEqual(report["warnings"][0]["kind"], "mark-unresolved")
        self.assertIsNone(report["items"][0]["measured"])
        code, decision = self.run_gate()
        self.assertEqual(code, 0, decision)
        self.assertEqual(decision["evidenceAudit"]["unresolvedVerticalAnchors"], 1)
        self.assertEqual(decision["evidenceAudit"]["warnings"][0]["kind"], "mark-unresolved")
        self.assertEqual(path.read_bytes(), original, "the gate must preserve canonical warning evidence")
        # Missing, unrelated, or wrong-anchor warnings cannot excuse placeholders.
        for warnings in ([], [{"kind": "text-fallback", "layoutMark": "synthetic-scope", "note": "Synthetic"}],
                         [{"kind": "mark-unresolved", "layoutMark": "another-anchor", "note": "Synthetic"}]):
            report["warnings"] = warnings
            path.write_text(json.dumps(report))
            with self.subTest(warnings=warnings):
                self.assertEqual(self.run_gate()[0], 2)

    def test_unrelated_and_foreign_round_or_score_report_is_rejected(self):
        self.completed()
        path = Path(self.state["phases"]["3"]["report"])
        path.write_text("Unrelated report")
        self.assertEqual(self.run_gate()[0], 2)
        for key, bad in (("round", 2), ("overallScore", 12), ("reportFile", str(self.root / "foreign.md"))):
            original = self.evaluation[key]
            self.evaluation[key] = bad
            self.write_report()
            with self.subTest(key=key):
                self.assertEqual(self.run_gate()[0], 2)
            self.evaluation[key] = original

    def test_render_unknown_payload_is_not_a_canonical_measurement(self):
        self.completed()
        path = self.round_dir / "render-diff.json"
        report = json.loads(path.read_text())
        report["items"][0].update(expected={"unrelated": "payload"}, measured={"unrelated": 123})
        path.write_text(json.dumps(report))
        self.assertEqual(self.run_gate()[0], 2)

    def test_canonical_decimal_score_and_custom_report_path_are_supported(self):
        self.completed()
        custom = self.root / "custom-evidence/evaluation.md"
        custom.parent.mkdir()
        self.state["phases"]["3"].update(report=str(custom), score=94.2)
        self.evaluation.update(reportFile=str(custom), overallScore=94.2)
        for dimension in self.evaluation["dimensions"].values():
            dimension.update(score=94.2, deduction=5.8)
        self.save()
        self.write_report()
        code, result = self.run_gate()
        self.assertEqual(code, 0, result)

    def test_concurrent_claim_is_exclusive_and_restart_cannot_be_overwritten(self):
        preload = Path(self.temp.name) / "claim-barrier.cjs"
        arrived = Path(self.temp.name) / "arrived"
        release = Path(self.temp.name) / "release"
        preload.write_text("const fs=require('fs'); const rename=fs.renameSync; fs.renameSync=function(a,b){"
                           "if(String(b).endsWith('.active-workflow.json')){fs.writeFileSync(process.env.TEST_ARRIVED,'ready');"
                           "while(!fs.existsSync(process.env.TEST_RELEASE)){Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10)}}"
                           "return rename.call(fs,a,b)}")
        args = [shutil.which("node"), "--require", str(preload), str(GATE), "--repo-root", str(self.root),
                "--page-dir", self.page, "--started-at", self.started, "--action", "claim", "--session-id", "first-native-session"]
        first = subprocess.Popen(args, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", ""), "TEST_ARRIVED": str(arrived), "TEST_RELEASE": str(release)})
        try:
            deadline = time.monotonic() + 5
            while not arrived.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(arrived.exists())
            self.assertEqual(self.run_gate("claim", "second-native-session")[0], 2)
            before = self.marker_file.read_bytes(), self.state_file.read_bytes()
            checkpoint = PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"
            restart = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                     cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(restart.returncode, 0)
            self.assertEqual(before, (self.marker_file.read_bytes(), self.state_file.read_bytes()))
        finally:
            release.touch()
            out, err = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, (out, err))
        self.assertEqual(json.loads(self.marker_file.read_text())["ownerSessionId"], "first-native-session")
        # Retrying the restart after the lock is released succeeds normally.
        restart = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                 cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(restart.returncode, 0, restart.stderr)
        self.assertIsNone(json.loads(self.marker_file.read_text())["ownerSessionId"])

    def test_lock_cleanup_on_explicit_process_exit_and_rejected_checkpoint(self):
        guard = PLUGIN / "skills/d-d2c/scripts/lib/d2c-workflow-guard-core.cjs"
        script = "require(process.argv[1]).withWorkflowLock(process.argv[2],()=>process.exit(7))"
        result = subprocess.run([shutil.which("node"), "-e", script, str(guard), str(self.page_dir.parent)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        self.assertFalse(list(self.page_dir.parent.glob(".workflow-state.lock*")))
        self.assertEqual(self.run_gate("claim")[0], 0)

    def test_stale_marker_sync_cannot_replace_restarted_workflow(self):
        old_state = self.root / "old-state.json"
        old_state.write_bytes(self.state_file.read_bytes())
        checkpoint = PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"
        restart = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                 cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(restart.returncode, 0, restart.stderr)
        before = self.marker_file.read_bytes()
        guard = PLUGIN / "skills/d-d2c/scripts/lib/d2c-workflow-guard-core.cjs"
        script = ("const fs=require('fs'); try{require(process.argv[1]).syncActiveWorkflow({stateDir:process.argv[2],"
                  "pageDir:process.argv[3],state:JSON.parse(fs.readFileSync(process.argv[4])),stateFile:process.argv[5]})}"
                  "catch(e){process.stdout.write(e.message);process.exitCode=2}")
        stale = subprocess.run([shutil.which("node"), "-e", script, str(guard), str(self.page_dir.parent),
                               self.page, str(old_state), str(self.state_file)], capture_output=True, text=True, timeout=10)
        self.assertEqual(stale.returncode, 2, stale.stdout)
        self.assertEqual(before, self.marker_file.read_bytes())

    def test_mutating_checkpoint_locks_before_reading_its_state(self):
        checkpoint = PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"
        preload = Path(self.temp.name) / "state-read-barrier.cjs"
        arrived, release = Path(self.temp.name) / "arrived", Path(self.temp.name) / "release"
        preload.write_text("const fs=require('fs'); const read=fs.readFileSync; let paused=false;"
                           "fs.readFileSync=function(file,...args){const result=read.call(fs,file,...args);"
                           "if(!paused&&String(file).endsWith('workflow-state.json')){paused=true;fs.writeFileSync(process.env.TEST_ARRIVED,'ready');"
                           "while(!fs.existsSync(process.env.TEST_RELEASE)){Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10)}}return result}")
        first = subprocess.Popen([shutil.which("node"), "--require", str(preload), str(checkpoint),
                                  "--page-dir", self.page, "--pause", "user-cancelled", "--reason", "synthetic-first-operation"],
                                 cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", ""), "TEST_ARRIVED": str(arrived), "TEST_RELEASE": str(release)})
        try:
            deadline = time.monotonic() + 5
            while not arrived.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(arrived.exists())
            before = self.marker_file.read_bytes(), self.state_file.read_bytes()
            restart = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                     cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(restart.returncode, 0)
            self.assertEqual(before, (self.marker_file.read_bytes(), self.state_file.read_bytes()))
        finally:
            release.touch()
            out, err = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, (out, err))

    def test_killed_lock_owner_does_not_block_later_checkpoint_or_claim(self):
        guard = PLUGIN / "skills/d-d2c/scripts/lib/d2c-workflow-guard-core.cjs"
        checkpoint = PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"
        for signal in ("SIGKILL", "SIGTERM"):
            ready = Path(self.temp.name) / f"ready-{signal}"
            script = ("const fs=require('fs');require(process.argv[1]).withWorkflowLock(process.argv[2],()=>{"
                      "fs.writeFileSync(process.argv[3],'ready');while(true)Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10000)})")
            owner = subprocess.Popen([shutil.which("node"), "-e", script, str(guard), str(self.page_dir.parent), str(ready)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                if signal == "SIGKILL":
                    owner.kill()
                else:
                    owner.terminate()
                owner.communicate(timeout=5)
                restart = subprocess.run([shutil.which("node"), str(checkpoint), "--enter", "0", "--page-dir", self.page],
                                         cwd=self.root, capture_output=True, text=True, timeout=10)
                self.assertEqual(restart.returncode, 0, restart.stderr)
                self.state = json.loads(self.state_file.read_text())
                self.marker = json.loads(self.marker_file.read_text())
                self.started = self.state["phases"]["0"]["enteredAt"]
                self.assertEqual(self.run_gate("claim")[0], 0)
                self.assertFalse(list(self.page_dir.parent.glob(".workflow-state.lock*")))
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
