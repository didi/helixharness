"""Host adapter contract and bounded command execution for version updates."""

from __future__ import annotations

import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import eval_json
from process_runtime import kill_group, spawn_group

MARKETPLACE = "helixharness-marketplace"
PLUGIN_ID = "helix@helixharness-marketplace"
PLUGIN_NAME = "helix"
INSPECT_TIMEOUT_SECONDS = 8.0
MARKETPLACE_TIMEOUT_SECONDS = 20.0
INSTALL_TIMEOUT_SECONDS = 60.0
STDOUT_LIMIT_BYTES = 4 * 1024 * 1024
STDERR_LIMIT_BYTES = 256 * 1024


class UpdateHostError(ValueError):
    """Raised when a host cannot provide trustworthy update facts."""


@dataclass(frozen=True)
class CommandResult:
    """Bounded result from a host CLI invocation."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class InstallStep:
    """One stable, retryable native host update step."""

    step_id: str
    command: tuple[str, ...]


class BoundedCommandRunner:
    """Run host commands with wall-clock and in-memory output bounds."""

    def __init__(
        self,
        *,
        stdout_limit: int = STDOUT_LIMIT_BYTES,
        stderr_limit: int = STDERR_LIMIT_BYTES,
    ) -> None:
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            kill_group(process.pid)
        except OSError:
            if process.poll() is None:
                process.kill()

    def run(self, command: Sequence[str], *, timeout: float) -> CommandResult:
        argv = [str(item) for item in command]
        if argv:
            argv[0] = shutil.which(argv[0]) or argv[0]
        try:
            process = spawn_group(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise UpdateHostError(f"host command could not start: {' '.join(argv)}") from exc

        stdout = bytearray()
        stderr = bytearray()
        overflow: list[str] = []
        terminate_lock = threading.Lock()

        def drain(stream: Any, target: bytearray, limit: int, label: str) -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        return
                    remaining = limit - len(target)
                    if remaining > 0:
                        target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.append(label)
                        with terminate_lock:
                            self._terminate(process)
                        return
            finally:
                stream.close()

        threads = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, self.stdout_limit, "stdout"),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, self.stderr_limit, "stderr"),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with terminate_lock:
                self._terminate(process)
            process.wait()
            for thread in threads:
                thread.join(timeout=1)
            raise UpdateHostError(
                f"host command timed out after {timeout:g}s: {' '.join(argv)}"
            ) from exc
        self._terminate(process)
        for thread in threads:
            thread.join(timeout=1)
        if overflow:
            raise UpdateHostError(
                f"host command {overflow[0]} exceeded its limit: {' '.join(argv)}"
            )
        result = CommandResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
        if returncode != 0:
            raise UpdateHostError(
                f"host command failed with exit {returncode}: {' '.join(argv)}"
            )
        return result

    def run_json(self, command: Sequence[str], *, timeout: float) -> Any:
        result = self.run(command, timeout=timeout)
        try:
            return eval_json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise UpdateHostError(
                f"host command returned invalid JSON: {' '.join(str(item) for item in command)}"
            ) from exc


def same_path(left: Any, right: Path) -> bool:
    try:
        return Path(str(left)).expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return False


def project_contains(project_path: Any, repo_root: Path) -> bool:
    try:
        project = Path(str(project_path)).expanduser().resolve()
        repo = repo_root.expanduser().resolve()
    except OSError:
        return False
    return project == repo or project in repo.parents


class UpdateHostAdapter(ABC):
    """Deep host seam used by the platform-independent update core."""

    host_id: str
    supported_scopes: frozenset[str]
    aggregate_scope_members: Mapping[str, frozenset[str]] = {}
    manifest_directory: str
    marketplace_manifest_directory: str

    def entry_policy(self, plugin_root: Path) -> dict[str, Any] | None:
        """Optional execution-channel restriction; existing hosts remain unchanged."""
        return None

    def validate_scope(self, scope: str) -> None:
        if scope not in self.supported_scopes:
            allowed = ", ".join(sorted(self.supported_scopes))
            raise UpdateHostError(
                f"invalid {self.host_id} update scope: {scope}; expected one of {allowed}"
            )

    def validate_live_scope(
        self,
        scope: str,
        installation: Mapping[str, Any],
    ) -> None:
        """校验聚合更新范围，不让共享核心猜测宿主范围名称。"""

        self.validate_scope(scope)
        expected = self.aggregate_scope_members.get(scope)
        if expected is None:
            return
        raw_available = installation.get("available_scopes")
        if (
            not isinstance(raw_available, list)
            or any(not isinstance(item, str) or not item for item in raw_available)
            or frozenset(raw_available) != expected
        ):
            raise UpdateHostError(
                f"{self.host_id} 聚合更新范围不完整: {scope}; "
                f"期望 {', '.join(sorted(expected))}"
            )

    @abstractmethod
    def state_home(self) -> Path:
        """Return the host-owned location for HelixHarness private update state."""

    @abstractmethod
    def inspect_installation(
        self,
        repo_root: Path,
        plugin_root: Path,
        runner: BoundedCommandRunner,
        plugin_list_data: Any = None,
    ) -> dict[str, Any]:
        """Return the selected formal installation after path/version checks."""

    @abstractmethod
    def marketplace_root(
        self,
        runner: BoundedCommandRunner,
        marketplace_list_data: Any = None,
    ) -> Path:
        """Return the unique formal marketplace root for this host."""

    @abstractmethod
    def refresh_marketplace(self, runner: BoundedCommandRunner) -> AbstractContextManager[Path]:
        """Yield a refreshed policy snapshot without updating installed plugins."""

    @abstractmethod
    def install_steps(
        self, scope: str, runner: BoundedCommandRunner
    ) -> tuple[InstallStep, ...]:
        """Return stable native update steps for retryable execution."""

    def manifest_path(self, plugin_root: Path) -> Path:
        return plugin_root / self.manifest_directory / "plugin.json"

    def marketplace_manifest_path(self, marketplace_root: Path) -> Path:
        """Locate the host's marketplace metadata inside the selected source."""

        root = marketplace_root.resolve()
        directory = Path(self.marketplace_manifest_directory)
        if directory.is_absolute() or ".." in directory.parts:
            raise UpdateHostError("marketplace manifest directory escapes its root")
        path = (root / directory / "marketplace.json").resolve()
        if not path.is_relative_to(root):
            raise UpdateHostError("marketplace manifest path escapes its root")
        return path

    def declared_version(self, plugin_root: Path) -> Any:
        """The version the executing plugin declares for itself.

        Defaults to the plugin manifest's ``version``; a host may override this
        to read a different authoritative source. Claude Code reads the version
        from marketplace.json (the marketplace manages plugin version), while
        Codex keeps reading its own plugin manifest.
        """
        return self._read_json(self.manifest_path(plugin_root)).get("version")

    def volatile_check_cache_paths(
        self, plugin_root: Path
    ) -> tuple[Path | None, Path | None]:
        """Return host-owned positive/negative caches for transient checks.

        Hosts that do not need a volatile fallback inherit the disabled seam so
        the shared update core never has to branch on a concrete host id.
        """

        del plugin_root
        return None, None

    def update_binding(self, snapshot_root: Path | None, installation: Mapping[str, Any], runner: BoundedCommandRunner) -> dict[str, Any] | None:
        """Optionally bind an immutable deployment source to the checked policy."""
        del snapshot_root, installation, runner
        return None

    def validate_update_binding(self, binding: Any) -> None:
        if binding is not None:
            raise UpdateHostError(f"{self.host_id} does not support an update binding")

    def prepare_update_binding(self, binding: Any) -> str:
        """Read-only pre-consent validation; return additional prompt context."""
        self.validate_update_binding(binding)
        return ""

    def cache_binding_matches_installation(self, binding: Any, installation: Mapping[str, Any]) -> bool:
        del binding, installation
        return True

    def validate_install_resume(self, authorization: Mapping[str, Any], *, plugin_root: Path) -> bool:
        """Opt into cache-independent resume only with independent transaction evidence.

        Native commands that may select the latest release keep strict cache
        validation. An adapter must prove it can resume the approved target.
        """
        del authorization, plugin_root
        return False

    def execute_install_step(self, step: InstallStep, runner: BoundedCommandRunner, *, authorization: Mapping[str, Any], plugin_root: Path) -> None:
        """Execute an authorized step. Legacy adapters keep their native command."""
        del authorization, plugin_root
        runner.run(step.command, timeout=INSTALL_TIMEOUT_SECONDS)

    def verify_installation(self, authorization: Mapping[str, Any], runner: BoundedCommandRunner, *, plugin_root: Path) -> dict[str, Any]:
        """Optional host verification before consuming the one-time authorization."""
        del authorization, runner, plugin_root
        return {}
