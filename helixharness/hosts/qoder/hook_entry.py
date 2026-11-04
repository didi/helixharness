#!/usr/bin/env python3
"""Qoder lifecycle event adapter.

Qoder 分发表面通过插件目录携带的 Skill 绑定本宿主清单；本入口只转换会话事件，
不猜测平台、不读取其他宿主的 hooks 或安装缓存。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from host_runtime import HostRegistry  # noqa: E402
from lifecycle_service import handle_event  # noqa: E402


SUPPORTED_EVENTS = frozenset({"SessionStart", "SessionEnd"})
SESSION_MARKER = Path(".helixharness") / "host-session" / "qoder.json"


def persist_session_marker(payload: dict) -> None:
    """Record the session identity for the telemetry adapter.

    The marker holds only host identifiers and paths; it carries no prompt,
    transcript text or credentials. Writes are best-effort and must never block
    a session.
    """

    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    if not isinstance(cwd, str) or not cwd.strip():
        return
    if not isinstance(session_id, str) or not session_id.strip():
        return
    repo = Path(cwd).expanduser()
    try:
        if not repo.is_dir():
            return
        target = repo / SESSION_MARKER
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            return
        record = {
            "session_id": session_id.strip(),
            "session_cwd": str(repo.resolve()),
        }
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript.strip():
            record["transcript_path"] = transcript.strip()
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        return


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event not in SUPPORTED_EVENTS:
            return 0
        if event == "SessionStart":
            persist_session_marker(payload)
        manifest = HostRegistry.discover(ROOT / "hosts").get("qoder")
        if manifest is None:
            return 0
        handle_event(manifest, payload)
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
