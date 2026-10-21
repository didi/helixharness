"""Launch Codex app-server with an explicit credential-safe environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

_PASSTHROUGH_NAMES = {
    "CODEX_ACCESS_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_CA_CERTIFICATE",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PATH",
    "JAVA_HOME",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "PATHEXT",
    "HELIXHARNESS_EVAL_GRADLE_HOME",
}


def isolated_gradle_home(codex_home: Path) -> Path:
    """Return the writable Gradle home owned by one disposable Episode."""

    return codex_home / "gradle-user-home"


def sanitized_environment(
    source: Mapping[str, str],
    *,
    codex_home: Path,
    gradle_home_path: Path | None = None,
) -> dict[str, str]:
    """Keep only runtime essentials and explicitly supported automation secrets."""

    environment = {name: source[name] for name in _PASSTHROUGH_NAMES if source.get(name)}
    environment["CODEX_HOME"] = str(codex_home)
    environment["CODEX_SQLITE_HOME"] = str(codex_home)
    environment["HOME"] = str(codex_home)
    gradle_home = gradle_home_path or isolated_gradle_home(codex_home)
    gradle_home.mkdir(parents=True, exist_ok=True)
    environment["GRADLE_USER_HOME"] = str(gradle_home)
    if gradle_home_path is not None:
        environment["HELIXHARNESS_EVAL_GRADLE_HOME"] = str(gradle_home)
    temp_directory = codex_home / "tmp"
    temp_directory.mkdir(parents=True, exist_ok=True)
    environment["TMPDIR"] = str(temp_directory)
    environment["JAVA_TOOL_OPTIONS"] = f"-Djava.io.tmpdir={temp_directory}"
    return environment


def main(argv: list[str] | None = None) -> int:
    """Replace this helper with the pinned Codex runtime process."""

    arguments = list(argv if argv is not None else sys.argv[1:])
    if not arguments:
        raise SystemExit("codex runtime path is required")
    codex_bin = Path(arguments[0]).resolve()
    codex_home_text = os.environ.get("CODEX_HOME", "")
    if not codex_home_text:
        raise SystemExit("isolated CODEX_HOME is required")
    environment = sanitized_environment(
        os.environ,
        codex_home=Path(codex_home_text).resolve(),
        gradle_home_path=(
            Path(os.environ["HELIXHARNESS_EVAL_GRADLE_HOME"]).resolve()
            if os.environ.get("HELIXHARNESS_EVAL_GRADLE_HOME")
            else None
        ),
    )
    os.execve(
        str(codex_bin),
        [str(codex_bin), *arguments[1:]],
        environment,
    )
    return 0  # pragma: no cover - os.execve replaces the helper process


if __name__ == "__main__":
    raise SystemExit(main())
