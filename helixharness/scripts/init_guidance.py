#!/usr/bin/env python3
"""Render, merge or check only Init's managed command navigation block.

This is a mechanical project-entry operation, not Init's semantic validator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from host_runtime import HostContractError, HostRegistry
from host_runtime.project_config import load_project_config


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
START = "<!-- helix:commands:start -->"
END = "<!-- helix:commands:end -->"
MODES = ("service", "frontend", "fullstack", "greenfield", "workspace")
LEGACY_SINGLE = (
    "## 常用命令\n\n"
    "`/helix:init`、`/helix:propose`、`/helix:apply`、`/helix:archive`、\n"
    "`/helix:ut`、`/helix:bug-fix`、`/helix:update`。\n"
    "`propose --loop` 按顺序执行 Propose → Apply → Archive。\n"
)
LEGACY_WORKSPACE = (
    "## 常用命令\n\n"
    "`/helix:init --workspace` 初始化根目录。`/helix:propose <feature> <requirement-or-path>`\n"
    "建立需求；`--loop` 串联 Propose → Apply → Archive。`apply` 与 `archive` 按 Router 的 Workspace\n"
    "范围调度子仓；其他命令的适用范围仍以各自 Router 和完整命令为准。\n"
)


class GuidanceError(ValueError):
    """Navigation cannot safely be generated, updated or accepted."""


def _lines(text: str):
    """Yield source offsets and fence context without rewriting user bytes."""
    offset = 0
    fence: tuple[str, int] | None = None
    for source in text.splitlines(keepends=True):
        line = source.rstrip("\r\n")
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        fenced = fence is not None
        if match:
            marks, tail = match.groups()
            if fence is None:
                fence = (marks[0], len(marks))
            elif marks[0] == fence[0] and len(marks) >= fence[1] and not tail.strip():
                fence = None
            fenced = True
        yield offset, line, fenced
        offset += len(source)


def _managed_span(text: str) -> tuple[int, int] | None:
    markers = []
    for offset, line, fenced in _lines(text):
        if "<!-- helix:commands:" in line:
            if fenced or line not in (START, END):
                raise GuidanceError("init_guidance_ambiguous_marker")
            markers.append((offset, line))
    if not markers:
        return None
    if len(markers) != 2 or markers[0][1] != START or markers[1][1] != END:
        raise GuidanceError("init_guidance_invalid_boundaries")
    return markers[0][0], markers[1][0] + len(END)


def _command_headings(text: str) -> list[int]:
    return [offset for offset, line, fenced in _lines(text)
            if not fenced and re.match(r"^ {0,3}#{1,6}\s+常用命令", line)]


def _legacy_span(text: str, mode: str) -> tuple[int, int] | None:
    headings = _command_headings(text)
    if not headings:
        return None
    if len(headings) != 1:
        raise GuidanceError("init_guidance_custom_commands_conflict")
    start = headings[0]
    # Only the old, literal bundled block under a Helix heading is owned.
    title = "HelixHarness Workspace" if mode == "workspace" else "HelixHarness 导航"
    owned = False
    for offset, line, fenced in _lines(text[:start]):
        if fenced:
            continue
        if re.fullmatch(r"#{1,2} " + re.escape(title), line):
            owned = True
        elif line.startswith("# "):
            owned = False
    end = len(text)
    for offset, line, fenced in _lines(text):
        if offset > start and not fenced and re.match(r"^ {0,3}#{1,2}\s", line):
            end = offset
            break
    legacy = LEGACY_WORKSPACE if mode == "workspace" else LEGACY_SINGLE
    if not owned or text[start:end].replace("\r\n", "\n").rstrip("\n") != legacy.rstrip("\n"):
        raise GuidanceError("init_guidance_custom_commands_conflict")
    # Preserve the surrounding whitespace, including the old block's final LF.
    return start, start + len(text[start:end].rstrip("\r\n"))


def _context(platform: str, mode: str):
    if mode not in MODES:
        raise GuidanceError("unsupported_init_mode")
    manifest = HostRegistry.discover(PLUGIN_ROOT / "hosts").get(platform)
    if manifest is None:
        raise GuidanceError("unsupported_host")
    config = load_project_config(manifest)
    relative = config.workspace_entry_template if mode == "workspace" else config.entry_template
    template = PLUGIN_ROOT / relative
    if not template.resolve().is_relative_to(PLUGIN_ROOT.resolve()):
        raise GuidanceError("init_guidance_template_escapes_plugin")
    text = template.read_text(encoding="utf-8")
    span = _managed_span(text)
    if span is None or _command_headings(text[:span[0]] + text[span[1]:]):
        raise GuidanceError("init_guidance_template_invalid")
    return config, text[span[0]:span[1]]


def check_text(text: str, expected: str) -> None:
    span = _managed_span(text)
    if span is None:
        raise GuidanceError("init_guidance_missing")
    if _command_headings(text[:span[0]] + text[span[1]:]):
        raise GuidanceError("init_guidance_custom_commands_conflict")
    if text[span[0]:span[1]].replace("\r\n", "\n") != expected:
        raise GuidanceError("init_guidance_outdated_or_incorrect")


def merge_text(text: str, expected: str, mode: str) -> str:
    span = _managed_span(text)
    if span is not None:
        if _command_headings(text[:span[0]] + text[span[1]:]):
            raise GuidanceError("init_guidance_custom_commands_conflict")
    else:
        span = _legacy_span(text, mode)
    if span is not None:
        result = text[:span[0]] + expected + text[span[1]:]
    else:
        separator = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
        result = text + separator + expected + "\n"
    check_text(result, expected)
    return result


def project_guidance(repo: Path, platform: str, mode: str, action: str) -> dict:
    if action not in ("render", "merge", "check"):
        raise GuidanceError("unsupported_guidance_action")
    config, expected = _context(platform, mode)
    if action == "render":
        return {"status": "rendered", "project_entry": config.entry_file, "block": expected}
    root = repo.expanduser().resolve()
    if not root.is_dir():
        raise GuidanceError("repository_directory_missing")
    target = root / config.entry_file
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise GuidanceError("init_guidance_unsafe_entry")
    original = target.read_bytes() if target.exists() else None
    text = original.decode("utf-8") if original is not None else ""
    if action == "check":
        check_text(text, expected)
        return {"status": "pass", "project_entry": config.entry_file}
    merged = merge_text(text, expected, mode).encode("utf-8")
    changed = original != merged
    if changed:
        # All navigation checks finish before the first filesystem mutation.
        fd, temporary = tempfile.mkstemp(prefix=".helixharness-guidance-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(merged)
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink() or (target.read_bytes() if target.exists() else None) != original:
                raise GuidanceError("init_guidance_entry_changed")
            os.chmod(temporary, (target.stat().st_mode & 0o777) if original is not None else 0o644)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"status": "merged", "changed": changed, "project_entry": config.entry_file}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("action", choices=("render", "merge", "check"))
    args = parser.parse_args()
    try:
        result = project_guidance(args.repo_root, args.platform, args.mode, args.action)
    except (OSError, UnicodeError, ValueError, HostContractError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
