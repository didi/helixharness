"""Cursor managed-local distribution; never impersonates a native marketplace API."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlparse

from .base import (
    MARKETPLACE,
    MARKETPLACE_TIMEOUT_SECONDS,
    PLUGIN_ID,
    PLUGIN_NAME,
    BoundedCommandRunner,
    InstallStep,
    UpdateHostAdapter,
    UpdateHostError,
)

RECEIPT = ".helixharness-managed.json"
REGISTRATION_KEYS = {"schema_version", "kind", "install_id", "install_root", "source", "ref", "plugin_source"}
BINDING_KEYS = {"schema_version", "kind", "install_id", "registration_sha256", "install_root", "source", "ref", "plugin_source", "current_tree", "target_commit", "target_tree", "target_version"}
RECEIPT_KEYS = {"schema_version", "install_id", "registration_sha256", "version", "commit", "tree_sha256"}
DISTRIBUTION_DIRS = {".claude-plugin", ".codex-plugin", ".cursor-plugin", "agents", "commands", "hosts", "reference", "scripts", "skills", "templates", "workflows"}
DISTRIBUTION_FILES = {"AGENTS.md", "CLAUDE.md", "LICENSE", "NOTICE", "PLUGIN_USAGE.md", "README.md", "README.zh-CN.md", "SKILL.md", "THIRD_PARTY_NOTICES.md", ".gitignore"}
EXCLUDED_PARTS = {".git", ".env", ".cursor", ".claude", ".codex", ".agents", ".aws", "output", "temp", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv"}
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024


def _json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("invalid metadata file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("metadata must be an object")
        return data
    except (OSError, ValueError) as exc:
        raise UpdateHostError("Cursor managed-local metadata is unavailable or invalid") from exc


def _digest(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _distributed(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts or any(part in EXCLUDED_PARTS or part.startswith(".env") for part in parts):
        return False
    if relative.name in {"credentials.json", ".npmrc", RECEIPT} or relative.suffix in {".pyc", ".log"}:
        return False
    return (len(parts) == 1 and parts[0] in DISTRIBUTION_FILES) or (len(parts) > 1 and parts[0] in DISTRIBUTION_DIRS)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(dirs)
        for name in dirs:
            path = base / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_symlink():
                raise UpdateHostError("managed distribution contains a symlink")
            if name != "__pycache__" and (name in EXCLUDED_PARTS or name.startswith(".env") or relative.parts[0] not in DISTRIBUTION_DIRS):
                raise UpdateHostError("managed installation contains unmanaged paths; preserve them outside the installation before updating")
        for name in sorted(files):
            path = base / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if relative.as_posix() == RECEIPT or ("__pycache__" in relative.parts and relative.suffix == ".pyc"):
                continue
            if not _distributed(relative):
                raise UpdateHostError("managed installation contains unmanaged files; preserve them outside the installation before updating")
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise UpdateHostError("managed distribution contains an invalid file")
            digest.update(relative.as_posix().encode() + b"\0")
            digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _exchange(left: Path, right: Path) -> None:
    """Atomic directory exchange; no delete/copy fallback is safe here."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        call = library.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = call(os.fsencode(left), os.fsencode(right), 2)  # RENAME_SWAP
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        call = library.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = call(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # RENAME_EXCHANGE
    else:
        raise UpdateHostError("atomic managed-local update requires macOS or Linux directory exchange")
    if result != 0:
        raise UpdateHostError(f"atomic managed-local exchange failed (errno {ctypes.get_errno()})")


def plugin_version(plugin_root: Path) -> str:
    data = _json(plugin_root / ".cursor-plugin/plugin.json")
    value = data.get("version")
    if data.get("name") != PLUGIN_NAME or not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?", value):
        raise UpdateHostError("Cursor plugin version manifest is invalid")
    return value


def _validate_bundle(root: Path) -> None:
    """Validate packaged references as data; never execute candidate plugin code."""
    def resource(value: Any) -> Path:
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in PurePosixPath(value).parts:
            raise UpdateHostError("Cursor distribution has an invalid resource path")
        path = root / value
        if path.is_symlink() or not path.exists() or not path.resolve().is_relative_to(root.resolve()):
            raise UpdateHostError("Cursor distribution is missing a required packaged resource")
        return path
    manifest = _json(root / ".cursor-plugin/plugin.json")
    for key in ("skills", "hooks", "commands", "agents", "rules"):
        declared = manifest.get(key)
        for item in declared if isinstance(declared, list) else ([declared] if isinstance(declared, str) else []):
            resource(item)
    host = _json(resource("hosts/cursor/host.json"))
    if host.get("host_id") != "cursor" or host.get("host_api_version") not in {"1.1", "1.2"}:
        raise UpdateHostError("Cursor distribution has an invalid Host Kit")
    adapters = host.get("adapters")
    if not isinstance(adapters, dict):
        raise UpdateHostError("Cursor distribution has no Host Kit adapters")
    for reference in adapters.values():
        if not isinstance(reference, str):
            raise UpdateHostError("Cursor distribution has an invalid Host Kit adapter")
        if reference.startswith("resource:"):
            resource(reference.removeprefix("resource:"))
        elif reference.startswith("python:"):
            module = reference.removeprefix("python:").split(":", 1)[0]
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
                raise UpdateHostError("Cursor distribution has an invalid adapter module")
            resource("scripts/" + module.replace(".", "/") + ".py")
    projection = _json(resource("hosts/cursor/command-projection.json"))
    expected = {"init", "propose", "apply", "archive", "ut", "bug-fix", "update", "status", "config"}
    entries = projection.get("entrypoints")
    if not isinstance(entries, dict) or set(entries) != expected:
        raise UpdateHostError("Cursor distribution must include all nine command entries")
    for name, pair in entries.items():
        expected_pair = [f"hosts/cursor/skills/helix-{name}/SKILL.md", f"commands/{name}.md"]
        if pair != expected_pair:
            raise UpdateHostError("Cursor distribution redirects a canonical command")
        for value in pair:
            resource(value)


class CursorUpdateAdapter(UpdateHostAdapter):
    host_id = "cursor"
    supported_scopes = frozenset({"local"})
    manifest_directory = ".cursor-plugin"
    marketplace_manifest_directory = ".cursor-plugin"

    def __init__(self, normalize_version: Callable[[Any], str], read_json: Callable[[Path], dict[str, Any]], *, home: Path | None = None):
        self._normalize_version = normalize_version
        self._read_json = read_json
        self._home = (home or Path(os.environ.get("HELIXHARNESS_CURSOR_HOME") or Path.home() / ".cursor")).expanduser().resolve()
        self._snapshots: dict[str, dict[str, Any]] = {}

    def entry_policy(self, plugin_root: Path) -> dict[str, Any] | None:
        root = plugin_root.expanduser().absolute()
        cache = self._home / "plugins/cache/helixharness-marketplace/helix"
        native_namespace = self._home / "plugins/cache"
        if root.parent != cache or not re.fullmatch(r"[0-9a-f]{40}", root.name):
            if root == native_namespace or native_namespace in root.parents:
                raise UpdateHostError("unrecognized Cursor native cache root")
            return None
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise UpdateHostError("Cursor native installation must not use symlinks")
        manifest = self._read_json(root / ".cursor-plugin/plugin.json")
        if manifest.get("name") != "helix":
            raise UpdateHostError("Cursor native manifest does not identify helix")
        version = self._normalize_version(manifest.get("version"))
        return {"state": "unavailable", "update_status": "unknown",
                "reason": "installation_update_not_supported",
                "warning": "WARN:cursor_native_automatic_update_unsupported",
                "next_action": "continue_with_warning", "platform": "cursor",
                "installation_kind": "cursor-native", "current_version": version,
                "cache_revision": root.name, "plugin_root": str(root),
                "automatic_update_supported": False,
                "manual_steps": ["Review a new Git commit and explicitly replace the project binding, or update through the owning Cursor marketplace.",
                                 "Reload Window, start a new Agent, and verify the actual Skill and Hook roots."]}

    def state_home(self) -> Path:
        return self._home

    @property
    def registration_path(self) -> Path:
        return self._home / "helixharness/managed-install.json"

    @property
    def install_root(self) -> Path:
        return self._home / "plugins/local/helix"

    def _safe_paths(self) -> None:
        for relative in ("plugins", "plugins/local", "plugins/local/helix", "helixharness", "helixharness/rollback", "helixharness/transactions", "helixharness/managed-install.json", "helixharness/managed-install.pending.json"):
            if (self._home / relative).is_symlink():
                raise UpdateHostError("Cursor managed-local paths must not be symlinks")

    @staticmethod
    def _source(source: Any, ref: Any) -> None:
        if not isinstance(source, str) or not source or any(char in source for char in "\r\n\0") or source.startswith("-"):
            raise UpdateHostError("invalid managed-local Git source")
        try:
            parsed = urlparse(source)
            allowed = Path(source).is_absolute() or (parsed.scheme in {"https", "ssh"} and bool(parsed.hostname)) or bool(re.fullmatch(r"[\w.-]+@[\w.-]+:[^\s]+", source))
        except ValueError as exc:
            raise UpdateHostError("invalid managed-local Git URL") from exc
        if not allowed or parsed.password is not None or parsed.query or parsed.fragment or (parsed.scheme == "https" and parsed.username is not None):
            raise UpdateHostError("managed-local source must be a credential-free Git URL or absolute local Git path")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref) or ".." in ref:
            raise UpdateHostError("invalid managed-local Git ref")

    def _registration(self) -> dict[str, Any]:
        self._safe_paths()
        data = _json(self.registration_path)
        return self._validate_registration(data)

    def _validate_registration(self, data: dict[str, Any]) -> dict[str, Any]:
        if set(data) != REGISTRATION_KEYS or data.get("schema_version") != "1.0" or data.get("kind") != "helix-managed-local":
            raise UpdateHostError("Cursor installation is not registered as Helix managed-local")
        if data["install_root"] != str(self.install_root) or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("install_id"))):
            raise UpdateHostError("managed-local registration identity does not match the designated installation")
        self._source(data["source"], data["ref"])
        source = PurePosixPath(str(data["plugin_source"]))
        if source.is_absolute() or ".." in source.parts or str(source) != data["plugin_source"]:
            raise UpdateHostError("managed-local plugin source escapes its Git tree")
        return data

    def _receipt(self, registration: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
        root = root or self.install_root
        receipt = _json(root / RECEIPT)
        if set(receipt) != RECEIPT_KEYS or receipt.get("schema_version") != "1.0" or receipt.get("install_id") != registration["install_id"] or receipt.get("registration_sha256") != _digest(registration):
            raise UpdateHostError("managed-local installation receipt does not match its registration")
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(receipt.get("commit"))) or receipt.get("tree_sha256") != _tree_digest(root):
            raise UpdateHostError("managed-local installed files differ from the recorded Git distribution")
        if self._normalize_version(plugin_version(root)) != receipt.get("version"):
            raise UpdateHostError("managed-local installed version differs from its receipt")
        return receipt

    def inspect_installation(self, repo_root: Path, plugin_root: Path, runner: BoundedCommandRunner, plugin_list_data: Any = None) -> dict[str, Any]:
        del repo_root, runner
        if plugin_list_data is not None:
            raise UpdateHostError("Cursor managed-local does not accept a fabricated native plugin catalog")
        registration = self._registration()
        if plugin_root.resolve() != self.install_root or plugin_root.is_symlink():
            raise UpdateHostError("executing Cursor plugin is not the registered managed-local installation; marketplace caches are never adopted")
        receipt = self._receipt(registration)
        return {"platform": self.host_id, "plugin_id": PLUGIN_ID, "current_version": receipt["version"], "current_scope": "local", "current_source": MARKETPLACE, "available_scopes": ["local"], "installation_kind": "helix-managed-local", "plugin_root": str(self.install_root), "install_id": registration["install_id"], "registration_sha256": _digest(registration), "tree_sha256": receipt["tree_sha256"]}

    def marketplace_root(self, runner: BoundedCommandRunner, marketplace_list_data: Any = None) -> Path:
        del runner, marketplace_list_data
        raise UpdateHostError("Cursor managed-local policy requires an isolated Git snapshot")

    @contextmanager
    def _snapshot(self, source: str, ref: str, runner: BoundedCommandRunner) -> Iterator[tuple[Path, dict[str, str]]]:
        self._source(source, ref)
        with tempfile.TemporaryDirectory(prefix="helix-cursor-source-") as temporary:
            base = Path(temporary).resolve()
            git = base / "git"
            git_args = ["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=", "-c", "protocol.ext.allow=never"]
            runner.run([*git_args, "init", "--quiet", str(git)], timeout=MARKETPLACE_TIMEOUT_SECONDS)
            runner.run([*git_args, "-C", str(git), "fetch", "--depth=1", "--", source, ref], timeout=MARKETPLACE_TIMEOUT_SECONDS)
            commit = runner.run([*git_args, "-C", str(git), "rev-parse", "FETCH_HEAD^{commit}"], timeout=MARKETPLACE_TIMEOUT_SECONDS).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                raise UpdateHostError("Git snapshot did not identify an immutable commit")
            archive = base / "source.tar"
            runner.run([*git_args, "-C", str(git), "archive", "--format=tar", f"--output={archive}", commit], timeout=MARKETPLACE_TIMEOUT_SECONDS)
            if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise UpdateHostError("managed-local distribution exceeds its size bound")
            root = base / "snapshot"
            root.mkdir()
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                metadata = next((item for item in members if item.name == ".cursor-plugin/marketplace.json"), None)
                if metadata is None or not metadata.isfile() or metadata.size > 1024 * 1024:
                    raise UpdateHostError("Git source lacks a Cursor marketplace manifest")
                stream = tar.extractfile(metadata)
                try:
                    marketplace = json.loads(stream.read()) if stream else None
                except (ValueError, UnicodeError) as exc:
                    raise UpdateHostError("invalid Cursor marketplace JSON in Git source") from exc
                plugins = marketplace.get("plugins") if isinstance(marketplace, dict) else None
                matches = [item for item in plugins if isinstance(item, dict) and item.get("name") == PLUGIN_NAME] if isinstance(plugins, list) else []
                if not isinstance(marketplace, dict) or marketplace.get("name") != MARKETPLACE or len(matches) != 1:
                    raise UpdateHostError("Git source is not the HelixHarness marketplace")
                plugin_source = PurePosixPath(str(matches[0].get("source", "")))
                if plugin_source.is_absolute() or ".." in plugin_source.parts or not matches[0].get("source"):
                    raise UpdateHostError("Git plugin source escapes the marketplace")
                plugin_root = root / str(plugin_source)
                plugin_root.mkdir(parents=True, exist_ok=True)
                seen: set[str] = set()
                for member in members:
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise UpdateHostError("Git archive contains an escaping path")
                    try:
                        relative = path.relative_to(plugin_source)
                    except ValueError:
                        continue
                    if not _distributed(relative) or member.isdir():
                        continue
                    if not member.isfile() or member.size > MAX_FILE_BYTES or relative.as_posix() in seen:
                        raise UpdateHostError("Git distribution contains an unsupported link, duplicate or oversized file")
                    seen.add(relative.as_posix())
                    target = plugin_root / relative.as_posix()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = tar.extractfile(member)
                    if content is None:
                        raise UpdateHostError("Git distribution file is unreadable")
                    target.write_bytes(content.read())
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                marketplace_target = root / ".cursor-plugin/marketplace.json"
                marketplace_target.parent.mkdir(parents=True, exist_ok=True)
                marketplace_target.write_text(json.dumps(marketplace), encoding="utf-8")
            version = self._normalize_version(plugin_version(plugin_root))
            from version_update import _load_policy
            try:
                policy = _load_policy(plugin_root / "reference/version-policy.json")
            except ValueError as exc:
                raise UpdateHostError("Cursor source version policy is invalid") from exc
            if version != self._normalize_version(matches[0].get("version")) or version != self._normalize_version(policy.get("latest_version")):
                raise UpdateHostError("Cursor source manifest and version policy disagree")
            _validate_bundle(plugin_root)
            yield root, {"commit": commit, "plugin_source": str(plugin_source), "version": version, "tree_sha256": _tree_digest(plugin_root)}

    @contextmanager
    def refresh_marketplace(self, runner: BoundedCommandRunner) -> Iterator[Path]:
        registration = self._registration()
        with self._snapshot(registration["source"], registration["ref"], runner) as (root, snapshot):
            if snapshot["plugin_source"] != registration["plugin_source"]:
                raise UpdateHostError("managed-local source layout changed; explicit reinstallation is required")
            self._snapshots[str(root)] = snapshot
            try:
                yield root
            finally:
                self._snapshots.pop(str(root), None)

    def update_binding(self, snapshot_root: Path | None, installation: Mapping[str, Any], runner: BoundedCommandRunner) -> dict[str, Any]:
        del runner
        registration = self._registration()
        snapshot = self._snapshots.get(str(snapshot_root))
        if snapshot is None or installation["registration_sha256"] != _digest(registration):
            raise UpdateHostError("managed-local source changed during version checking")
        return {"schema_version": "1.0", "kind": "helix-managed-local", "install_id": registration["install_id"], "registration_sha256": _digest(registration), "install_root": registration["install_root"], "source": registration["source"], "ref": registration["ref"], "plugin_source": registration["plugin_source"], "current_tree": installation["tree_sha256"], "target_commit": snapshot["commit"], "target_tree": snapshot["tree_sha256"], "target_version": snapshot["version"]}

    def validate_update_binding(self, binding: Any) -> None:
        if not isinstance(binding, dict) or set(binding) != BINDING_KEYS or binding.get("schema_version") != "1.0" or binding.get("kind") != "helix-managed-local":
            raise UpdateHostError("Cursor managed-local update requires a checked immutable source binding")
        for key in ("registration_sha256", "current_tree", "target_tree"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(binding[key])):
                raise UpdateHostError("invalid managed-local source digest")
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(binding["target_commit"])):
            raise UpdateHostError("invalid managed-local source commit")
        self._normalize_version(binding["target_version"])

    def _live_binding(self, binding: Any, *, allow_target: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        self.validate_update_binding(binding)
        registration = self._registration()
        if _digest(registration) != binding["registration_sha256"] or any(registration[key] != binding[key] for key in ("install_id", "install_root", "source", "ref", "plugin_source")):
            raise UpdateHostError("managed-local registration or source changed after version checking")
        receipt = self._receipt(registration)
        current = receipt["tree_sha256"] == binding["current_tree"]
        target = receipt["tree_sha256"] == binding["target_tree"] and receipt["commit"] == binding["target_commit"] and receipt["version"] == binding["target_version"]
        if not current and not (allow_target and target):
            raise UpdateHostError("managed-local installed tree changed after authorization")
        return registration, receipt

    def prepare_update_binding(self, binding: Any) -> str:
        self._live_binding(binding)
        return f"安装方式：Helix 管理的 Cursor local 安装（非原生市场更新）\n安装位置：{binding['install_root']}\n来源：{binding['source']}\n已核验 commit：{binding['target_commit']}\n文件摘要：{binding['target_tree']}\n更新后须 Developer: Reload Window 或重启 Cursor。"

    def cache_binding_matches_installation(self, binding: Any, installation: Mapping[str, Any]) -> bool:
        return isinstance(binding, dict) and binding.get("install_id") == installation.get("install_id") and binding.get("registration_sha256") == installation.get("registration_sha256") and binding.get("current_tree") == installation.get("tree_sha256")

    def install_steps(self, scope: str, runner: BoundedCommandRunner) -> tuple[InstallStep, ...]:
        del runner
        self.validate_scope(scope)
        return (InstallStep("managed-local-atomic-exchange", ()),)

    def _update_transaction(self, authorization: Mapping[str, Any]) -> tuple[Path, Path, dict[str, str]]:
        challenge = str(authorization.get("challenge_id", ""))
        if not re.fullmatch(r"upd_[0-9a-f]{24}", challenge):
            raise UpdateHostError("invalid managed-local update transaction")
        backup = self._home / "helixharness/rollback" / challenge
        transaction = self._home / "helixharness/transactions" / (challenge + ".json")
        # This independent receipt is written before deployment. A later check
        # may refresh its cache, but cannot change what this transaction approved.
        approved = {key: authorization.get(key) for key in (
            "challenge_id", "platform", "scope", "current_version", "target_version",
            "update_status", "created_at", "expires_at", "confirmation_source",
            "authorized_at", "update_binding", "approved_releases",
        )}
        return backup, transaction, {
            "binding_sha256": _digest(authorization["update_binding"]),
            "authorization_sha256": _digest(approved), "backup": str(backup),
        }

    def validate_install_resume(self, authorization: Mapping[str, Any], *, plugin_root: Path) -> bool:
        if (authorization.get("state") != "installing"
                or authorization.get("confirmation_source") != "interactive-user"
                or "approved_releases" not in authorization):
            return False
        self.validate_scope(str(authorization.get("scope")))
        if plugin_root.is_symlink() or plugin_root.resolve() != self.install_root:
            raise UpdateHostError("managed-local resume source differs from the registered installation")
        binding = authorization.get("update_binding")
        self._live_binding(binding, allow_target=True)
        if binding["target_version"] != authorization.get("target_version"):
            raise UpdateHostError("managed-local resume target differs from authorization")
        _, transaction, expected = self._update_transaction(authorization)
        if not transaction.exists():
            return False
        if _json(transaction) != expected:
            raise UpdateHostError("managed-local transaction no longer matches the approved update")
        return True

    def _verify_target(self, registration: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
        if self._registration() != registration:
            raise UpdateHostError("managed-local registration changed during deployment")
        receipt = self._receipt(registration)
        _validate_bundle(self.install_root)
        if any(receipt[key] != binding[target] for key, target in (("version", "target_version"), ("commit", "target_commit"), ("tree_sha256", "target_tree"))):
            raise UpdateHostError("managed-local post-install verification failed")

    def execute_install_step(self, step: InstallStep, runner: BoundedCommandRunner, *, authorization: Mapping[str, Any], plugin_root: Path) -> None:
        from version_update import _lock
        with _lock(self.registration_path.with_name("managed-install.guard")):
            self._execute_install_step(step, runner, authorization=authorization, plugin_root=plugin_root)

    def _execute_install_step(self, step: InstallStep, runner: BoundedCommandRunner, *, authorization: Mapping[str, Any], plugin_root: Path) -> None:
        if authorization.get("state") != "installing" or authorization.get("confirmation_source") != "interactive-user":
            raise UpdateHostError("managed-local deployment requires the confirmed update state machine")
        if step.step_id != "managed-local-atomic-exchange" or plugin_root.resolve() != self.install_root:
            raise UpdateHostError("managed-local install step or executing source is invalid")
        binding = authorization.get("update_binding")
        registration, receipt = self._live_binding(binding, allow_target=True)
        if binding["target_version"] != authorization.get("target_version"):
            raise UpdateHostError("managed-local target version differs from authorization")
        backup, transaction, transaction_data = self._update_transaction(authorization)
        if transaction.exists() and _json(transaction) != transaction_data:
            raise UpdateHostError("managed-local transaction no longer matches the approved update")
        if receipt["tree_sha256"] == binding["target_tree"] and receipt["commit"] == binding["target_commit"]:
            if not transaction.exists():
                raise UpdateHostError("managed-local deployment has no independent transaction receipt")
            self._verify_target(registration, binding)
            return  # Recovery after a process stopped following the atomic exchange.
        self._safe_paths()
        backup.parent.mkdir(parents=True, exist_ok=True)
        transaction.parent.mkdir(parents=True, exist_ok=True)
        if backup.is_symlink():
            raise UpdateHostError("managed-local rollback directory must not be a symlink")
        if backup.exists():
            if _json(transaction) != transaction_data:
                raise UpdateHostError("managed-local transaction already has an unrecognized rollback directory")
            shutil.rmtree(backup)  # Retry staging after interruption; live old tree was verified above.
        _write_json(transaction, transaction_data)
        swapped = False
        try:
            with self._snapshot(registration["source"], binding["target_commit"], runner) as (root, snapshot):
                if any(snapshot[key] != binding[target] for key, target in (("commit", "target_commit"), ("version", "target_version"), ("tree_sha256", "target_tree"), ("plugin_source", "plugin_source"))):
                    raise UpdateHostError("approved managed-local Git snapshot no longer matches its binding")
                shutil.copytree(root / snapshot["plugin_source"], backup)
            new_receipt = {"schema_version": "1.0", "install_id": registration["install_id"], "registration_sha256": _digest(registration), "version": binding["target_version"], "commit": binding["target_commit"], "tree_sha256": binding["target_tree"]}
            _write_json(backup / RECEIPT, new_receipt)
            self._live_binding(binding)
            self._receipt(registration, backup)
            _exchange(self.install_root, backup)
            swapped = True
            self._verify_target(registration, binding)
        except BaseException:
            if swapped:
                _exchange(self.install_root, backup)
            if backup.exists():
                shutil.rmtree(backup)
            raise

    def verify_installation(self, authorization: Mapping[str, Any], runner: BoundedCommandRunner, *, plugin_root: Path) -> dict[str, Any]:
        del runner
        if plugin_root.resolve() != self.install_root:
            raise UpdateHostError("Cursor source binding changed during installation")
        binding = authorization.get("update_binding")
        registration, _ = self._live_binding(binding, allow_target=True)
        _, transaction, expected = self._update_transaction(authorization)
        if _json(transaction) != expected:
            raise UpdateHostError("managed-local transaction no longer matches the approved update")
        self._verify_target(registration, binding)
        return {"installation_kind": "helix-managed-local", "verified_version": binding["target_version"], "verified_commit": binding["target_commit"], "restart_required": True, "reload_instruction": "Developer: Reload Window or restart Cursor; start a new Agent session."}

    @staticmethod
    def _initial_sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _initial_rename_exclusive(source: Path, destination: Path) -> None:
        """Publish without replacing even an empty directory or unrelated metadata."""
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin" and hasattr(library, "renamex_np"):
            call = library.renamex_np
            call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            result = call(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
        elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
            call = library.renameat2
            call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = call(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
        else:
            raise UpdateHostError("initial managed-local installation requires an exclusive atomic rename")
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))

    def _move_initial_candidate(self, staged: Path) -> None:
        self._initial_rename_exclusive(staged, self.install_root)
        self._initial_sync_directory(self.install_root.parent)
        self._initial_sync_directory(staged.parent)

    def _publish_initial_registration(self, registration: Mapping[str, Any], staged: Path) -> None:
        self._safe_paths()
        metadata = staged.parent / "registration.json"
        if (metadata.exists() or metadata.is_symlink()) and _json(metadata) != registration:
            raise UpdateHostError("initial recovery registration candidate changed")
        if self.registration_path.exists():
            if self._registration() != registration:
                raise UpdateHostError("initial recovery cannot replace another registration")
            metadata.unlink(missing_ok=True)
        else:
            if _json(metadata) != registration:
                raise UpdateHostError("initial recovery registration candidate changed")
            self._initial_rename_exclusive(metadata, self.registration_path)
        self._initial_sync_directory(self.registration_path.parent)

    def _initial_journal(self, pending: Path) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        journal = _json(pending)
        if set(journal) != {"schema_version", "registration", "staged_root", "receipt"} or journal["schema_version"] != "1.0" or not isinstance(journal["registration"], dict) or not isinstance(journal["receipt"], dict):
            raise UpdateHostError("invalid managed-local initial recovery journal")
        registration = self._validate_registration(journal["registration"])
        staged = self.registration_path.parent / ("initial-" + registration["install_id"]) / "plugin"
        if journal["staged_root"] != str(staged) or staged.parent.is_symlink() or staged.is_symlink():
            raise UpdateHostError("initial recovery staging path escapes managed storage")
        receipt = journal["receipt"]
        if set(receipt) != RECEIPT_KEYS or receipt.get("schema_version") != "1.0" or receipt.get("install_id") != registration["install_id"] or receipt.get("registration_sha256") != _digest(registration):
            raise UpdateHostError("initial recovery receipt does not match its registration")
        return registration, staged, receipt

    def _recover_initial_locked(self, pending: Path) -> dict[str, Any]:
        self._safe_paths()
        registration, staged, expected_receipt = self._initial_journal(pending)
        if self.registration_path.exists() and self._registration() != registration:
            raise UpdateHostError("initial recovery cannot replace another registration")
        metadata = staged.parent / "registration.json"
        if (not self.registration_path.exists() or metadata.exists() or metadata.is_symlink()) and _json(metadata) != registration:
            raise UpdateHostError("initial recovery registration candidate changed")
        if self.install_root.exists() and staged.exists():
            raise UpdateHostError("initial recovery found an occupied installation and a staged candidate")
        candidate = self.install_root if self.install_root.exists() else staged
        receipt = self._receipt(registration, candidate)
        if receipt != expected_receipt:
            raise UpdateHostError("initial recovery candidate differs from the authorized Git snapshot")
        _validate_bundle(candidate)
        if candidate == staged:
            self._move_initial_candidate(staged)
        # Repeat validation immediately before publication, including after a recovered rename.
        self._safe_paths()
        if self._receipt(registration) != expected_receipt:
            raise UpdateHostError("initial recovery candidate changed during publication")
        self._initial_sync_directory(self.install_root.parent)
        self._publish_initial_registration(registration, staged)
        if staged.parent.exists():
            staged.parent.rmdir()  # Never remove unexpected files beside the candidate.
        pending.unlink()
        try:
            self._initial_sync_directory(pending.parent)
        except OSError:
            # Registration and installation are already durable. A stale journal after a
            # crash is safe to recover; a cleanup fsync failure does not undo the commit.
            pass
        return {"installed": True, "recovered": True, "installation_kind": "helix-managed-local", "plugin_root": str(self.install_root), "version": receipt["version"], "commit": receipt["commit"], "restart_required": True}

    def initialize(self, source: str, ref: str, runner: BoundedCommandRunner, *, user_confirmed: bool) -> dict[str, Any]:
        if not user_confirmed:
            raise UpdateHostError("managed-local installation requires explicit user confirmation")
        library = ctypes.CDLL(None)
        if not ((sys.platform == "darwin" and hasattr(library, "renamex_np")) or (sys.platform.startswith("linux") and hasattr(library, "renameat2"))):
            raise UpdateHostError("managed-local installation requires macOS or Linux atomic directory exchange")
        self._safe_paths()
        pending = self.registration_path.with_name("managed-install.pending.json")
        from version_update import _lock
        with _lock(self.registration_path.with_name("managed-install.guard")):
            self._safe_paths()
            if pending.exists():
                registration, _, _ = self._initial_journal(pending)
                if registration["source"] != source or registration["ref"] != ref:
                    raise UpdateHostError("initial retry source or ref differs from the confirmed installation")
                return self._recover_initial_locked(pending)
            if self.install_root.exists() or self.registration_path.exists():
                raise UpdateHostError("managed-local initial install never adopts or overwrites an existing installation")
            with self._snapshot(source, ref, runner) as (root, snapshot):
                registration = {"schema_version": "1.0", "kind": "helix-managed-local", "install_id": uuid.uuid4().hex, "install_root": str(self.install_root), "source": source, "ref": ref, "plugin_source": snapshot["plugin_source"]}
                receipt = {"schema_version": "1.0", "install_id": registration["install_id"], "registration_sha256": _digest(registration), "version": snapshot["version"], "commit": snapshot["commit"], "tree_sha256": snapshot["tree_sha256"]}
                self.install_root.parent.mkdir(parents=True, exist_ok=True)
                staging_directory = self.registration_path.parent / ("initial-" + registration["install_id"])
                staging_directory.mkdir()
                staged = staging_directory / "plugin"
                try:
                    shutil.copytree(root / snapshot["plugin_source"], staged)
                    _write_json(staged / RECEIPT, receipt)
                    self._receipt(registration, staged)
                    _write_json(staging_directory / "registration.json", registration)
                    self._safe_paths()
                    if self.install_root.exists() or self.registration_path.exists() or pending.exists():
                        raise UpdateHostError("managed-local installation appeared while preparing initial install")
                    _write_json(pending, {"schema_version": "1.0", "registration": registration, "staged_root": str(staged), "receipt": receipt})
                    result = self._recover_initial_locked(pending)
                except BaseException:
                    # A failed directory fsync may still have published the journal. Once it
                    # is visible, retain its candidate through ordinary errors and exits.
                    if not pending.exists() and staging_directory.exists():
                        shutil.rmtree(staging_directory)
                    raise
        result.pop("recovered", None)
        return result

    def recover_initial(self, *, user_confirmed: bool) -> dict[str, Any]:
        if not user_confirmed:
            raise UpdateHostError("initial installation recovery requires explicit user confirmation")
        from version_update import _lock
        self._safe_paths()
        pending = self.registration_path.with_name("managed-install.pending.json")
        with _lock(self.registration_path.with_name("managed-install.guard")):
            return self._recover_initial_locked(pending)
