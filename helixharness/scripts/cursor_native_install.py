#!/usr/bin/env python3
"""Configure a pinned Cursor project plugin; never write Cursor's native cache."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import urlsplit

import eval_json
from update_hosts.base import BoundedCommandRunner

KEY = "helixharness-marketplace/helix"
MISSING = {"absent": True}


def safe(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink path is not supported")
    return path.resolve()


def read(path):
    path = safe(path)
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("configuration must be a bounded JSON file")
    value = eval_json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return value


def fingerprint(path):
    path = safe(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def atomic(path, value):
    path = safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".helix-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_source(url, commit):
    if not isinstance(url, str) or any(c.isspace() for c in url) or "\x00" in url:
        raise ValueError("invalid Git source")
    parsed = urlsplit(url)
    https = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
    ssh = re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+", url)
    if not (https or ssh) or parsed.query or parsed.fragment:
        raise ValueError("use credential-free HTTPS or git@ source; local sources are not supported")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("an explicit full lowercase commit SHA is required")


def source_snapshot(url, commit, runner=None):
    """Inspect committed data, without executing candidate code or extracting it."""
    validate_source(url, commit)
    runner = runner or BoundedCommandRunner()
    with tempfile.TemporaryDirectory(prefix="helix-native-source-") as temporary:
        def git(*args):
            return runner.run(["git", "-c", "core.hooksPath=/dev/null", "-C", temporary, *args], timeout=20).stdout
        git("init", "--bare", "--quiet")
        git("fetch", "--quiet", "--depth=1", "--", url, commit)
        if git("rev-parse", "FETCH_HEAD^{commit}").strip() != commit:
            raise ValueError("source commit mismatch")
        market = eval_json.loads(git("show", commit + ":.cursor-plugin/marketplace.json"))
        if not isinstance(market, dict) or market.get("name") != "helixharness-marketplace":
            raise ValueError("unexpected marketplace")
        entries = market.get("plugins", [])
        if not isinstance(entries, list):
            raise ValueError("invalid marketplace plugins")
        matches = [v for v in entries if isinstance(v, dict) and v.get("name") == "helix"]
        if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
            raise ValueError("expected one relative helix plugin source")
        raw = matches[0]["source"]
        part = PurePosixPath(raw)
        if not raw or part.is_absolute() or ".." in part.parts or "\\" in raw:
            raise ValueError("invalid plugin subdirectory")
        prefix = "" if str(part) == "." else str(part) + "/"
        files = {}
        for record in git("ls-tree", "-rz", "--full-tree", commit).split("\x00"):
            if not record:
                continue
            header, name = record.split("\t", 1)
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if not relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or "\\" in relative:
                raise ValueError("distribution path escapes plugin")
            mode, kind, digest = header.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError("plugin distribution contains links or submodules")
            if any(p.startswith(".env") or p in {".git", "output", "__pycache__"} for p in PurePosixPath(relative).parts):
                raise ValueError("plugin distribution contains non-public runtime files")
            files[relative] = digest
        if not files or len(files) > 5000:
            raise ValueError("invalid distribution size")
        required = {".cursor-plugin/plugin.json", "hosts/cursor/hooks.json", "hosts/cursor/host.json", "scripts/host_runtime_cli.py"}
        for command in ("init", "propose", "apply", "archive", "ut", "bug-fix", "update", "status", "config"):
            required.update({f"hosts/cursor/skills/helix-{command}/SKILL.md", f"commands/{command}.md"})
        if not required.issubset(files):
            raise ValueError("Cursor distribution is incomplete")
        manifest = eval_json.loads(git("show", commit + ":" + prefix + ".cursor-plugin/plugin.json"))
        if not isinstance(manifest, dict) or manifest.get("name") != "helix" or manifest.get("hooks") != "./hosts/cursor/hooks.json":
            raise ValueError("invalid Cursor plugin manifest")
        return {"binding": {"enabled": True, "gitUrl": url, "gitRef": commit, "gitPath": "./" + str(part) if prefix else "."}, "files": files}


class ProjectInstall:
    def __init__(self, project, home=None):
        self.project = safe(project)
        if not self.project.is_dir():
            raise ValueError("project directory does not exist")
        self.settings = safe(self.project / ".cursor/settings.json")
        self.home = safe(home or os.environ.get("HELIXHARNESS_CURSOR_HOME") or Path.home() / ".cursor")
        key = hashlib.sha256(str(self.project).encode()).hexdigest()
        self.state = self.home / "helixharness/native-project-installs" / key
        self.receipt = self.state / "binding.json"

    @contextmanager
    def locked(self):
        # Scope is the tested macOS/Linux desktop channel.
        import fcntl
        safe(self.state).mkdir(parents=True, exist_ok=True)
        lock = safe(self.state / "lock")
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def configuration(self):
        value = read(self.settings)
        if not isinstance(value.get("plugins", {}), dict):
            raise ValueError("plugins must be an object")
        if KEY in value.get("plugins", {}) and value["plugins"][KEY] == MISSING:
            raise ValueError("invalid existing Helix binding")
        return value

    def transaction(self):
        record = read(self.receipt)
        if record and (set(record) != {"phase", "old", "new", "settings_existed"}
                       or record["phase"] not in {"pending", "configured", "restored"}
                       or not isinstance(record["new"], dict)
                       or type(record["settings_existed"]) is not bool):
            raise ValueError("invalid native configuration recovery record")
        return record

    def conflicts(self):
        local = safe(self.home / "plugins/local")
        result = []
        if local.is_dir():
            for directory in local.iterdir():
                for name in (".cursor-plugin/plugin.json", ".claude-plugin/plugin.json", "plugin.json"):
                    path = directory / name
                    if path.exists() and read(path).get("name") == "helix":
                        result.append(str(directory)); break
        return result

    def configure(self, snapshot, confirmed=False, replace=False):
        if not confirmed:
            raise ValueError("configuration requires user confirmation")
        if self.conflicts():
            raise ValueError("Cursor Local helix conflict: move the owned copy outside plugins/local first; do not delete other hosts")
        with self.locked():
            initial = fingerprint(self.settings)
            config = self.configuration()
            old = config.get("plugins", {}).get(KEY, MISSING)
            target = snapshot["binding"]
            record = self.transaction()
            if record and record.get("phase") != "restored":
                if record.get("new") != target:
                    raise ValueError("restore the prior configuration transaction before replacing its pin")
                if old not in (record["old"], target):
                    raise ValueError("configuration changed outside this transaction")
            else:
                if old == target:
                    return {"status": "configured", "changed": False, "reload_required": True}
                if old != MISSING and not replace:
                    raise ValueError("different binding exists; explicit --replace is required")
                record = {"phase": "pending", "old": old, "new": target, "settings_existed": self.settings.exists()}
                atomic(self.receipt, record)
            config.setdefault("plugins", {})[KEY] = target
            if fingerprint(self.settings) != initial:
                raise ValueError("concurrent settings modification")
            atomic(self.settings, config)
            atomic(self.receipt, {**record, "phase": "configured"})
        return {"status": "configured", "reload_required": True, "session_loaded": "not_verified"}

    def restore(self, confirmed=False):
        if not confirmed:
            raise ValueError("restore requires user confirmation")
        with self.locked():
            record = self.transaction()
            if not record or record.get("phase") == "restored":
                return {"status": "unchanged"}
            initial = fingerprint(self.settings)
            config = self.configuration()
            current = config.get("plugins", {}).get(KEY, MISSING)
            if current not in (record["new"], record["old"]):
                raise ValueError("Helix binding changed externally; restore refused")
            plugins = config.setdefault("plugins", {})
            if record["old"] == MISSING:
                plugins.pop(KEY, None)
            else:
                plugins[KEY] = record["old"]
            if not plugins:
                config.pop("plugins")
            if fingerprint(self.settings) != initial:
                raise ValueError("concurrent settings modification")
            if not config and not record["settings_existed"]:
                self.settings.unlink(missing_ok=True)
                if self.settings.parent.exists():
                    fd = os.open(self.settings.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            else:
                atomic(self.settings, config)
            atomic(self.receipt, {**record, "phase": "restored"})
        return {"status": "restored", "reload_required": True}

    def check(self, snapshot, plugin_root):
        if self.configuration().get("plugins", {}).get(KEY) != snapshot["binding"]:
            raise ValueError("project binding mismatch")
        expected = self.home / "plugins/cache/helixharness-marketplace/helix" / snapshot["binding"]["gitRef"]
        root = safe(plugin_root)
        if root != expected or self.conflicts():
            raise ValueError("unexpected plugin root or Local conflict")
        for name, expected_hash in snapshot["files"].items():
            path = safe(root / name)
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("missing or oversized payload file")
            data = path.read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()
            if digest != expected_hash:
                raise ValueError("installed payload differs from pinned source")
        return {"status": "payload_verified", "files": len(snapshot["files"]), "session_loaded": "not_verified", "next_action": "reload_and_verify_status_and_hook_identity"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "configure", "check", "restore"))
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--source")
    parser.add_argument("--commit")
    parser.add_argument("--plugin-root")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--user-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        project = ProjectInstall(args.project_root)
        if args.action == "restore":
            result = project.restore(args.user_confirmed)
        else:
            if not args.source or not args.commit:
                raise ValueError("--source and --commit are required")
            snapshot = source_snapshot(args.source, args.commit)
            if args.action == "configure":
                result = project.configure(snapshot, args.user_confirmed, args.replace)
            elif args.action == "check":
                if not args.plugin_root:
                    raise ValueError("--plugin-root is required")
                result = project.check(snapshot, args.plugin_root)
            else:
                project.configuration()
                result = {"status": "planned", "binding": snapshot["binding"], "local_conflicts": project.conflicts(), "account_source": "verify_in_cursor_ui", "session_loaded": "not_verified"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        # Never echo unknown configuration values, credentials or Git output.
        print(json.dumps({"status": "error", "error": str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "native_configuration_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
