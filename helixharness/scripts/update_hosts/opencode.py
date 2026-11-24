"""OpenCode source-checkout updates, pinned to the exact consented Git commit."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .base import (MARKETPLACE, PLUGIN_ID, BoundedCommandRunner, InstallStep,
                   UpdateHostAdapter, UpdateHostError)


def detect_host(environment: Any) -> int | None:
    """Return a confidence score only for explicit OpenCode runtime markers."""

    if environment.get("OPENCODE_SESSION_ID"):
        return 100
    return 80 if environment.get("OPENCODE") else None


def plugin_version(plugin_root: Path) -> str:
    try:
        if (plugin_root / "package.json").is_symlink():
            raise ValueError()
        package = json.loads((plugin_root / "package.json").read_text(encoding="utf-8"))
        version = package.get("version")
        if (package.get("name") != "helixharness-opencode" or package.get("exports", {}).get(".") != "./opencode/index.ts" or
                not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)):
            raise ValueError()
        return version
    except (OSError, ValueError, AttributeError):
        raise UpdateHostError("OpenCode package version is unavailable") from None


class OpenCodeUpdateAdapter(UpdateHostAdapter):
    """Only clean, attached checkouts with an explicit upstream can be updated.

    Package-manager caches and detached pins are never guessed or rewritten.
    Checks fetch an isolated ref and inspect a disposable clone; installation
    performs one hook-free fast-forward to the commit in the authorization.
    """
    host_id = "opencode"
    supported_scopes = frozenset({"checkout"})
    manifest_directory = ".claude-plugin"
    marketplace_manifest_directory = ".claude-plugin"

    def __init__(self, normalize_version, read_json, *, home: Path | None = None):
        self._normalize_version = normalize_version
        self._read_json = read_json
        self._home = home or Path(os.environ.get("HELIXHARNESS_OPENCODE_HOME") or
                                 Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "opencode")
        self._live: dict[str, Any] | None = None
        self._target: str | None = None

    def state_home(self) -> Path:
        return self._home.expanduser().resolve()

    def declared_version(self, plugin_root: Path) -> str:
        return plugin_version(plugin_root)

    @staticmethod
    def _git(runner, root, *args, timeout=8):
        return runner.run(("git", "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
                           "-C", str(root), *args), timeout=timeout).stdout.strip()

    def inspect_installation(self, repo_root, plugin_root, runner, plugin_list_data=None):
        del repo_root, plugin_list_data
        plugin = plugin_root.expanduser().absolute()
        if any(item.is_symlink() for item in (plugin, *plugin.parents)):
            raise UpdateHostError("OpenCode checkout path must not use symlinks")
        root = Path(self._git(runner, plugin, "rev-parse", "--show-toplevel")).resolve()
        if not plugin.is_relative_to(root):
            raise UpdateHostError("OpenCode plugin is outside its checkout")
        if self._git(runner, root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise UpdateHostError("OpenCode checkout has local changes; update refused")
        branch = self._git(runner, root, "symbolic-ref", "--quiet", "--short", "HEAD")
        upstream = self._git(runner, root, "for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)", f"refs/heads/{branch}").split("\0")
        if len(upstream) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", upstream[0]) or not upstream[1].startswith("refs/heads/"):
            raise UpdateHostError("OpenCode checkout requires an explicit remote branch upstream")
        version = self._normalize_version(plugin_version(plugin))
        live = {"platform": self.host_id, "plugin_id": PLUGIN_ID, "current_version": version,
                "current_scope": "checkout", "current_source": MARKETPLACE,
                "available_scopes": ["checkout"], "plugin_root": str(plugin),
                "checkout_root": str(root), "plugin_relative": plugin.relative_to(root).as_posix(),
                "source_commit": self._git(runner, root, "rev-parse", "HEAD"),
                "branch": branch, "remote": upstream[0], "remote_ref": upstream[1]}
        self._live = live
        return live

    def marketplace_root(self, runner, marketplace_list_data=None):
        raise UpdateHostError("OpenCode updates require a fresh pinned source snapshot")

    @contextmanager
    def refresh_marketplace(self, runner):
        if self._live is None:
            raise UpdateHostError("OpenCode installation must be inspected first")
        live = self._live
        root = Path(live["checkout_root"])
        ref = "refs/helixharness-check/" + uuid.uuid4().hex
        try:
            self._git(runner, root, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                      live["remote"], f"{live['remote_ref']}:{ref}", timeout=20)
            target = self._git(runner, root, "rev-parse", "--verify", f"{ref}^{{commit}}")
            self._git(runner, root, "merge-base", "--is-ancestor", live["source_commit"], target)
            with tempfile.TemporaryDirectory(prefix="helix-opencode-policy-") as directory:
                snapshot = Path(directory) / "snapshot"
                runner.run(("git", "-c", "core.hooksPath=/dev/null", "clone", "--no-checkout", "--shared", "--quiet", "--", str(root), str(snapshot)), timeout=20)
                snapshot = snapshot.resolve()
                self._git(runner, snapshot, "checkout", "--quiet", "--detach", target, timeout=20)
                self._target = target
                yield snapshot
        finally:
            self._git(runner, root, "update-ref", "-d", ref)
            self._target = None

    def update_binding(self, snapshot_root, installation, runner):
        if self._target is None or snapshot_root is None:
            raise UpdateHostError("OpenCode target snapshot is missing")
        binding = {key: installation[key] for key in
                   ("checkout_root", "plugin_relative", "source_commit", "branch", "remote", "remote_ref")}
        binding.update({"kind": "opencode-checkout/1.0", "target_commit": self._target})
        self.validate_update_binding(binding)
        plugin = snapshot_root / binding["plugin_relative"]
        if plugin.is_symlink() or not plugin.resolve().is_relative_to(snapshot_root.resolve()):
            raise UpdateHostError("OpenCode target package escapes its checkout")
        plugin_version(plugin)
        entry = plugin / "opencode/index.ts"
        if not entry.is_file() or not entry.resolve().is_relative_to(plugin.resolve()):
            raise UpdateHostError("OpenCode target package entry is missing")
        return binding

    def validate_update_binding(self, binding):
        keys = {"kind", "checkout_root", "plugin_relative", "source_commit", "target_commit", "branch", "remote", "remote_ref"}
        if not isinstance(binding, dict) or set(binding) != keys or binding.get("kind") != "opencode-checkout/1.0":
            raise UpdateHostError("invalid OpenCode update binding")
        if any(not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) for value in binding.values()):
            raise UpdateHostError("invalid OpenCode update binding values")
        relative = Path(binding["plugin_relative"])
        if not Path(binding["checkout_root"]).is_absolute() or relative.is_absolute() or ".." in relative.parts:
            raise UpdateHostError("unsafe OpenCode update paths")
        for key in ("source_commit", "target_commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", binding[key]):
                raise UpdateHostError("OpenCode update requires immutable commit IDs")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", binding["remote"]) or not binding["remote_ref"].startswith("refs/heads/"):
            raise UpdateHostError("invalid OpenCode update upstream")

    def prepare_update_binding(self, binding):
        self.validate_update_binding(binding)
        return f"OpenCode source target: {binding['target_commit']}. Only a clean fast-forward is allowed; restart OpenCode afterwards."

    def cache_binding_matches_installation(self, binding, installation):
        try:
            self.validate_update_binding(binding)
        except UpdateHostError:
            return False
        return all(binding[key] == installation.get(key) for key in
                   ("checkout_root", "plugin_relative", "source_commit", "branch", "remote", "remote_ref"))

    def install_steps(self, scope, runner):
        self.validate_scope(scope)
        return (InstallStep("opencode-checkout-fast-forward", ()),)

    def execute_install_step(self, step, runner, *, authorization, plugin_root):
        if step.step_id != "opencode-checkout-fast-forward" or authorization.get("state") != "installing":
            raise UpdateHostError("OpenCode update requires an authorized installation transaction")
        binding = authorization.get("update_binding")
        self.validate_update_binding(binding)
        live = self.inspect_installation(plugin_root, plugin_root, runner)
        if live["source_commit"] == binding["target_commit"]:
            # A crash between the fast-forward and its receipt can be retried.
            self.verify_installation(authorization, runner, plugin_root=plugin_root)
            return
        if not self.cache_binding_matches_installation(binding, live):
            raise UpdateHostError("OpenCode checkout changed after authorization")
        root = Path(binding["checkout_root"])
        target = binding["target_commit"]
        prefix = "" if binding["plugin_relative"] == "." else binding["plugin_relative"] + "/"
        try:
            package = json.loads(self._git(runner, root, "show", f"{target}:{prefix}package.json"))
            if (package.get("name") != "helixharness-opencode" or package.get("version") != authorization.get("target_version") or
                    package.get("exports", {}).get(".") != "./opencode/index.ts"):
                raise ValueError()
        except (ValueError, AttributeError):
            raise UpdateHostError("OpenCode target version/package differs from authorization") from None
        self._git(runner, root, "merge-base", "--is-ancestor", binding["source_commit"], target)
        self._git(runner, root, "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", target, timeout=60)

    def verify_installation(self, authorization, runner, *, plugin_root):
        binding = authorization.get("update_binding")
        self.validate_update_binding(binding)
        live = self.inspect_installation(plugin_root, plugin_root, runner)
        if (any(live[key] != binding[key] for key in ("checkout_root", "plugin_relative", "branch", "remote", "remote_ref")) or
                live["source_commit"] != binding["target_commit"] or live["current_version"] != authorization["target_version"]):
            raise UpdateHostError("OpenCode installed revision/version verification failed")
        return {"target_commit": live["source_commit"], "current_version": live["current_version"], "restart_required": True}
