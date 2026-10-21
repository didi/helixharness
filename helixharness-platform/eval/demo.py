"""Generate clearly labeled synthetic HelixHarness telemetry for local demos."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from eval.stage_supplements import (
    LEDGER_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    StageSupplementStore,
)


def ensure_usage_demo(directory: Path) -> Path:
    """Materialize a stable synthetic ledger and supplements outside package source."""
    root = directory / "project"
    database = directory / "stage_supplements.db"
    ledger = root / "docs/eval/example-feature/eval-log.json"
    if not ledger.is_file() or not database.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        seed_demo(root, database)
    return root


def _workflow_log() -> dict:
    return {
        "schema": LEDGER_SCHEMA,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "feature": "example-feature",
        "project": {"name": "demo-service"},
        "requirement": {"id": "req_0123456789abcdef01234567", "name": "example-feature"},
        "created_at": "2026-08-01T00:00:00Z",
        "last_updated": "2026-08-01T00:10:00Z",
        "status": "archived",
        "entries": [
            {
                "entry_id": "entry-0123456789abcdef",
                "helixharness_version": "1.0.0",
                "command": "apply",
                "stage_kind": "coding",
                "attempt": 1,
                "trigger": "user_command",
                "platform": "codex",
                "session_hash": "session-0123456789abcdef0123456789abcdef",
                "started_at": "2026-08-01T00:00:00Z",
                "ended_at": "2026-08-01T00:10:00Z",
                "status": "completed",
                "end_reason": "workflow_completed",
                "actor": {"user_name": "Demo", "user_name_source": "git_config_local_user_name"},
                "checkpoint": {},
                "artifacts": ["docs/specs/active/example-feature/verify-report.md"],
                "quality": {"standards_verdict": "APPROVE", "spec_verdict": "APPROVE"},
            }
        ],
    }


def _workflow_supplement() -> dict:
    return {
        "schema": "helixharness-stage-supplement/1.0",
        "supplement_id": "supp_0123456789abcdef",
        "revision": 1,
        "supersedes": None,
        "entry_id": "entry-0123456789abcdef",
        "requirement_id": "req_0123456789abcdef01234567",
        "requirement_name": "example-feature",
        "feature": "example-feature",
        "session_hash": "session-0123456789abcdef0123456789abcdef",
        "platform": "codex",
        "usage": {
            "input_tokens": 30,
            "output_tokens": 12,
            "cached_input_tokens": 7,
            "model": "gpt-test",
        },
        "execution": {"tool_call_count": 2, "subagent_spawn_count": 0},
        "evidence": {
            "schema": "helixharness-evolution-evidence/1.0",
            "interactions": [
                {"role": "user", "text": "修复结算超时"},
                {"role": "assistant", "text": "已定位重试路径"},
            ],
            "interaction_summary": {"user_turns": 1, "assistant_messages": 1},
            "tools": {"succeeded": 2, "failed": 0},
            "failed_tools": [],
            "coverage": {"in_window": 4, "retained": 2},
        },
        "workflow_trace": {"schema": "helixharness-workflow-trace/1.0", "status": "collected"},
    }


def seed_demo(root: Path, database: Path) -> Path:
    ledger = _workflow_log()
    ledger["project"]["name"] = "Demo project (synthetic)"
    entries = []
    store = StageSupplementStore(database)
    for index, (command, stage) in enumerate(
        (("propose", "requirement"), ("apply", "coding"), ("archive", "archive")), 1
    ):
        entry = copy.deepcopy(ledger["entries"][0])
        entry.update(
            entry_id=f"entry-{index:016x}",
            command=command,
            stage_kind=stage,
            helixharness_version="1.0.0",
            started_at=f"2026-09-10T00:{(index - 1) * 10:02d}:00Z",
            ended_at=f"2026-09-10T00:{index * 10:02d}:00Z",
        )
        entry["actor"]["user_name"] = "Demo User (synthetic)"
        entries.append(entry)
        supplement = _workflow_supplement()
        supplement.update(supplement_id=f"supp_{index:016x}", entry_id=entry["entry_id"])
        supplement["usage"] = {
            "input_tokens": 10000 * index,
            "output_tokens": 2000 * index,
            "cached_input_tokens": 1000 * index,
            "model": "demo-model",
        }
        supplement["evidence"]["interactions"] = [
            {"role": "user", "text": "Synthetic demo request"},
            {"role": "assistant", "text": "Synthetic demo evidence"},
        ]
        store.ingest(supplement)
    ledger.update(entries=entries, created_at=entries[0]["started_at"], last_updated=entries[-1]["ended_at"])
    path = root / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    store.refresh_links([root])
    return path
