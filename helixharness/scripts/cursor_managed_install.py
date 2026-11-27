#!/usr/bin/env python3
"""Explicit installer for Helix-managed Cursor local distributions."""

from __future__ import annotations

import argparse
import json

from update_hosts.base import BoundedCommandRunner, UpdateHostError
from update_hosts.cursor import CursorUpdateAdapter
from version_update import _read_json, _version_text
from cli_stdio import configure_utf8_stdio


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--source", required=True, help="credential-free Git URL or absolute local Git path")
    install.add_argument("--ref", required=True, help="explicit branch, tag or commit to follow")
    install.add_argument("--user-confirmed", action="store_true")
    recovery = commands.add_parser("recover-initial")
    recovery.add_argument("--user-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        adapter = CursorUpdateAdapter(_version_text, _read_json)
        result = adapter.recover_initial(user_confirmed=args.user_confirmed) if args.command == "recover-initial" else adapter.initialize(args.source, args.ref, BoundedCommandRunner(), user_confirmed=args.user_confirmed)
    except (UpdateHostError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
