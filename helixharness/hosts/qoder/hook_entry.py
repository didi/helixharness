#!/usr/bin/env python3
"""Qoder lifecycle event adapter.

Qoder 分发表面通过插件目录携带的 Skill 绑定本宿主清单；本入口只转换会话事件，
不猜测平台、不读取其他宿主的 hooks 或安装缓存。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from host_runtime import HostRegistry  # noqa: E402
from lifecycle_service import handle_event  # noqa: E402


SUPPORTED_EVENTS = frozenset({"SessionStart", "SessionEnd"})


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if payload.get("hook_event_name") not in SUPPORTED_EVENTS:
            return 0
        manifest = HostRegistry.discover(ROOT / "hosts").get("qoder")
        if manifest is None:
            return 0
        handle_event(manifest, payload)
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
