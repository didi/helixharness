"""Sync HelixHarness usage telemetry from Git repositories into a local cache.

The WebUI reads local project roots that contain docs/eval/*/eval-log.json.
This script keeps those local roots up to date from remote Git repositories and
prints/records the resulting HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.feature_exclusions import FeatureExclusionStore  # noqa: E402
from eval.ledger_paths import confined_ledger_directory, ledger_files  # noqa: E402
from eval.stage_supplements import (  # noqa: E402
    LEDGER_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    StageSupplementStorageError,
    StageSupplementStore,
)

DEFAULT_CONFIG = PROJECT_ROOT / "eval" / "configs" / "usage_repos.yaml"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "eval" / ".cache"
SYNC_STATE_FILE = "sync_state.json"


def _merged_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged_env = os.environ.copy()
    merged_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        merged_env.update(env)
    return merged_env


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    merged_env = _merged_env(env)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged_env, check=True)


def _capture(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    merged_env = _merged_env(env)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _slug(value: str) -> str:
    value = value.rstrip("/")
    value = re.sub(r"\.git$", "", value)
    value = value.split("/")[-1] or "repo"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "repo"


def _repo_key(repo: dict[str, Any]) -> str:
    return _slug(str(repo.get("name") or repo.get("url") or "repo"))


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {path}")
    return data


def _repo_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    repos = config.get("repos", [])
    if not isinstance(repos, list):
        raise ValueError("config.repos must be a list")
    result = []
    for item in repos:
        if not isinstance(item, dict):
            raise ValueError("each repo item must be a mapping")
        if item.get("enabled", True) is False:
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            raise ValueError("repo.url is required")
        result.append(item)
    return result


def _load_state(cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / SYNC_STATE_FILE
    if not path.exists():
        return {
            "cache_schema_version": "1.0",
            "telemetry_schema_version": LEDGER_SCHEMA_VERSION,
            "repos": {},
        }
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {
            "cache_schema_version": "1.0",
            "telemetry_schema_version": LEDGER_SCHEMA_VERSION,
            "repos": {},
        }
    if not isinstance(data, dict) or data.get("cache_schema_version") != "1.0":
        return {
            "cache_schema_version": "1.0",
            "telemetry_schema_version": LEDGER_SCHEMA_VERSION,
            "repos": {},
        }
    data["telemetry_schema_version"] = LEDGER_SCHEMA_VERSION
    data.setdefault("repos", {})
    return data


def _write_state(cache_dir: Path, state: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    state["cache_schema_version"] = "1.0"
    state["telemetry_schema_version"] = LEDGER_SCHEMA_VERSION
    state["last_updated_at"] = _now_iso()
    with (cache_dir / SYNC_STATE_FILE).open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _remote_head(url: str, branch: str, env: dict[str, str]) -> str:
    ref = f"refs/heads/{branch}"
    output = _capture(["git", "ls-remote", "--heads", url, branch], env=env)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    raise RuntimeError(f"remote branch not found: {url} {branch}")


def _local_head(target: Path, env: dict[str, str]) -> str | None:
    if not (target / ".git").is_dir():
        return None
    try:
        return _capture(["git", "-C", str(target), "rev-parse", "HEAD"], env=env)
    except subprocess.CalledProcessError:
        return None


def _local_head_author(target: Path, env: dict[str, str]) -> str:
    """获取 HEAD commit 的提交人（姓名）。"""
    if not (target / ".git").is_dir():
        return ""
    try:
        return _capture(["git", "-C", str(target), "log", "-1", "--format=%an"], env=env)
    except subprocess.CalledProcessError:
        return ""


def _repo_dirty(target: Path, env: dict[str, str]) -> bool:
    if not (target / ".git").is_dir():
        return False
    try:
        return bool(_capture(["git", "-C", str(target), "status", "--porcelain"], env=env))
    except subprocess.CalledProcessError:
        return True


def _is_shallow(target: Path, env: dict[str, str]) -> bool:
    try:
        return _capture(["git", "-C", str(target), "rev-parse", "--is-shallow-repository"], env=env) == "true"
    except subprocess.CalledProcessError:
        return False


def _eval_inventory(
    target: Path,
    eval_dir: str,
    *,
    repository: str | None = None,
    excluded_features: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    root = confined_ledger_directory(target, eval_dir)
    inventory = {
        "eval_log_count": 0,
        "schema_2_log_count": 0,
        "valid_log_count": 0,
        "invalid_log_count": 0,
    }
    if not root.is_dir():
        return inventory
    exclusions = excluded_features or set()
    repository_name = repository or target.name
    for path in ledger_files(target, root):
        if (repository_name, path.parent.name) in exclusions:
            continue
        inventory["eval_log_count"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            inventory["invalid_log_count"] += 1
            continue
        entries_are_valid = isinstance(data, dict) and isinstance(data.get("entries"), list)
        if (
            entries_are_valid
            and data.get("schema") == LEDGER_SCHEMA
            and data.get("schema_version") == LEDGER_SCHEMA_VERSION
        ):
            inventory["schema_2_log_count"] += 1
            inventory["valid_log_count"] += 1
        else:
            inventory["invalid_log_count"] += 1
    return inventory


def _sync_repo(
    repo: dict[str, Any],
    config: dict[str, Any],
    cache_dir: Path,
    depth: int,
    state: dict[str, Any],
    excluded_features: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    url = str(repo["url"]).strip()
    branch = str(repo.get("branch") or "master").strip()
    name = str(repo.get("name") or _slug(url)).strip()
    target = cache_dir / _slug(name)
    if target.name in {".", "..", ".demo"} or target.resolve().parent != cache_dir.resolve():
        raise ValueError("repository cache target is reserved or escapes the cache directory")
    eval_dir = str(repo.get("eval_dir") or "docs/eval").strip()
    confined_ledger_directory(target, eval_dir)
    env = {}
    git_ssh_command = repo.get("git_ssh_command") or config.get("git_ssh_command")
    if git_ssh_command:
        env["GIT_SSH_COMMAND"] = str(git_ssh_command)

    target.parent.mkdir(parents=True, exist_ok=True)
    key = _repo_key(repo)
    repos_state = state.setdefault("repos", {})
    previous = repos_state.get(key, {}) if isinstance(repos_state.get(key), dict) else {}
    remote_commit = _remote_head(url, branch, env)
    local_commit = _local_head(target, env)

    if (target / ".git").is_dir():
        _run(["git", "-C", str(target), "remote", "set-url", "origin", url], env=env)
        if depth <= 0 and _is_shallow(target, env):
            _run(["git", "-C", str(target), "fetch", "--prune", "--unshallow", "origin", branch], env=env)
            local_commit = _local_head(target, env)
        dirty = _repo_dirty(target, env)
        if local_commit == remote_commit and not dirty:
            eval_present = (target / eval_dir).is_dir()
            inventory = _eval_inventory(
                target,
                eval_dir,
                repository=name,
                excluded_features=excluded_features,
            )
            repos_state[key] = {
                "name": name,
                "url": url,
                "branch": branch,
                "eval_dir": eval_dir,
                "last_commit": remote_commit,
                "last_commit_author": _local_head_author(target, env),
                "previous_commit": previous.get("last_commit"),
                "last_checked_at": _now_iso(),
                "last_changed_at": previous.get("last_changed_at"),
                "status": "unchanged",
                "changed": False,
                "eval_present": eval_present,
                "path": str(target),
                "telemetry_schema_version": LEDGER_SCHEMA_VERSION,
                **inventory,
            }
            return {
                "root": target,
                "name": name,
                "changed": False,
                "commit": remote_commit,
                "eval_present": eval_present,
                "eval_log_count": inventory["eval_log_count"],
                "schema_2_log_count": inventory["schema_2_log_count"],
                "valid_log_count": inventory["valid_log_count"],
                "invalid_log_count": inventory["invalid_log_count"],
                "status": "unchanged",
            }
        if dirty:
            print(f"[warn] {name}: local cache has changes; resetting to remote {branch}", file=sys.stderr)
        fetch_args = ["git", "-C", str(target), "fetch", "--prune"]
        if depth > 0:
            fetch_args.extend(["--depth", str(depth)])
        fetch_args.extend(["origin", branch])
        _run(fetch_args, env=env)
        _run(["git", "-C", str(target), "reset", "--hard", "FETCH_HEAD"], env=env)
        # clean 时排除 eval_dir，保留未纳入 Git 的本地 eval 遥测数据
        clean_args = ["git", "-C", str(target), "clean", "-fd", "-e", eval_dir]
        _run(clean_args, env=env)
        _run(["git", "-C", str(target), "checkout", "-B", branch, "FETCH_HEAD"], env=env)
    else:
        clone_args = ["git", "clone"]
        if depth > 0:
            clone_args.extend(["--depth", str(depth)])
        clone_args.extend(["--branch", branch, url, str(target)])
        _run(clone_args, env=env)

    synced_commit = _local_head(target, env) or remote_commit
    eval_present = (target / eval_dir).is_dir()
    inventory = _eval_inventory(
        target,
        eval_dir,
        repository=name,
        excluded_features=excluded_features,
    )
    changed_at = _now_iso()
    repos_state[key] = {
        "name": name,
        "url": url,
        "branch": branch,
        "eval_dir": eval_dir,
        "last_commit": synced_commit,
        "last_commit_author": _local_head_author(target, env),
        "previous_commit": previous.get("last_commit"),
        "last_checked_at": changed_at,
        "last_changed_at": changed_at,
        "status": "updated" if previous.get("last_commit") else "cloned",
        "changed": True,
        "eval_present": eval_present,
        "path": str(target),
        "telemetry_schema_version": LEDGER_SCHEMA_VERSION,
        **inventory,
    }
    if not (target / eval_dir).is_dir():
        print(f"[warn] {name}: missing {eval_dir}, skipped by WebUI", file=sys.stderr)
    return {
        "root": target,
        "name": name,
        "changed": True,
        "commit": synced_commit,
        "eval_present": eval_present,
        "eval_log_count": inventory["eval_log_count"],
        "schema_2_log_count": inventory["schema_2_log_count"],
        "valid_log_count": inventory["valid_log_count"],
        "invalid_log_count": inventory["invalid_log_count"],
        "status": "updated" if previous.get("last_commit") else "cloned",
    }


def _read_roots(cache_dir: Path) -> list[Path]:
    path = cache_dir / "roots.txt"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    roots = []
    for item in text.split(","):
        root = item.strip()
        if root:
            roots.append(Path(root))
    return roots


def _write_roots(cache_dir: Path, roots: list[Path]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    roots_text = ",".join(str(p) for p in roots)
    (cache_dir / "roots.txt").write_text(roots_text + "\n", encoding="utf-8")
    (cache_dir / "env.sh").write_text(
        f'export HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS="{roots_text}"\n',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync HelixHarness docs/eval telemetry from Git repositories.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="YAML config containing repos[].")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="Local cache directory for cloned repos.")
    parser.add_argument("--depth", type=int, default=0, help="Git clone/fetch depth; 0 keeps full history.")
    parser.add_argument("--print-env", action="store_true", help="Print export HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS=...")
    parser.add_argument(
        "--skip-ai",
        action="store_true",
        help="Refresh Git telemetry without waiting for optional AI insights.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    os.environ["HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE"] = str(cache_dir)
    config = _load_config(config_path)
    repos = _repo_entries(config)
    if not repos:
        _write_roots(cache_dir, [])
        print(f"[warn] no enabled repos in {config_path}", file=sys.stderr)
        return 0

    from eval.usage_first_seen import UsageFirstSeenStore

    admissions = UsageFirstSeenStore(cache_dir)
    previous_roots = _read_roots(cache_dir)
    admissions.bootstrap([str(root) for root in previous_roots])
    roots = []
    failures = 0
    failed_repos: dict[str, dict[str, Any]] = {}
    state = _load_state(cache_dir)
    started_at = _now_iso()
    counts = {
        "cloned": 0,
        "updated": 0,
        "unchanged": 0,
        "with_eval": 0,
        "valid_eval_log_count": 0,
        "invalid_eval_log_count": 0,
    }
    exclusions = FeatureExclusionStore(cache_dir / "feature_exclusions.db").active_pairs()
    for repo in repos:
        try:
            result = _sync_repo(repo, config, cache_dir, args.depth, state, exclusions)
        except subprocess.CalledProcessError as exc:
            failures += 1
            failed_repos[str(cache_dir / _repo_key(repo))] = repo
            name = str(repo.get("name") or repo.get("url") or "repo")
            print(f"[error] {name}: git sync failed with exit code {exc.returncode}", file=sys.stderr)
            continue
        except (RuntimeError, ValueError) as exc:
            failures += 1
            if not isinstance(exc, ValueError):
                failed_repos[str(cache_dir / _repo_key(repo))] = repo
            name = str(repo.get("name") or repo.get("url") or "repo")
            print(f"[error] {name}: {exc}", file=sys.stderr)
            continue
        status = str(result.get("status") or "cloned")
        if status in counts:
            counts[status] += 1
        valid_eval_log_count = int(result.get("valid_log_count") or 0)
        invalid_eval_log_count = int(result.get("invalid_log_count") or 0)
        if valid_eval_log_count > 0:
            counts["with_eval"] += 1
        counts["valid_eval_log_count"] += valid_eval_log_count
        counts["invalid_eval_log_count"] += invalid_eval_log_count
        eval_dir = str(repo.get("eval_dir") or "docs/eval").strip()
        root = result["root"]
        short_commit = str(result.get("commit") or "")[:12]
        print(f"[{result['status']}] {result['name']} {short_commit}")
        # 收录含 eval 遥测的仓库。
        if (root / eval_dir).is_dir():
            roots.append(root)

    roots_to_write = list(roots)
    if failures and previous_roots:
        seen = {str(p) for p in roots_to_write}
        preserved = []
        for previous_root in previous_roots:
            repo = failed_repos.get(str(previous_root))
            if repo is None or not previous_root.exists() or str(previous_root) in seen:
                continue
            if previous_root.resolve().parent != cache_dir.resolve() or previous_root.name == ".demo":
                continue
            try:
                _eval_inventory(previous_root, str(repo.get("eval_dir") or "docs/eval"))
            except (OSError, RuntimeError, ValueError):
                continue
            roots_to_write.append(previous_root)
            preserved.append(previous_root)
            seen.add(str(previous_root))
        if preserved:
            print(f"[warn] preserving {len(preserved)} previous root(s) after sync failure", file=sys.stderr)

    _write_roots(cache_dir, roots_to_write)
    # Consumers must see this synchronization's layout before linking/rendering.
    _write_state(cache_dir, state)
    supplement_db = Path(
        os.environ.get("HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH", str(cache_dir / "stage_supplements.db"))
    )
    try:
        finalized_count = StageSupplementStore(supplement_db).refresh_links(roots_to_write)
        print(f"async telemetry: finalized {finalized_count} new entry(s); retention cleanup checked")
    except StageSupplementStorageError as exc:
        print(f"[warn] async telemetry refresh skipped: {exc}", file=sys.stderr)
    from eval.usage_loader import aggregate_all, discover_dashboard_projects

    linked = StageSupplementStore(supplement_db).active_linked_records()
    admissions.observe(aggregate_all(discover_dashboard_projects([str(root) for root in roots_to_write], linked_supplements=linked))["features"])
    state["last_run"] = {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "total": len(repos),
        "succeeded": len(repos) - failures,
        "cloned": counts["cloned"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "with_eval": counts["with_eval"],
        "valid_eval_log_count": counts["valid_eval_log_count"],
        "invalid_eval_log_count": counts["invalid_eval_log_count"],
        "failed": failures,
    }
    _write_state(cache_dir, state)
    if args.skip_ai:
        print("ai insights: skipped by scheduled telemetry sync")
    else:
        try:
            from eval.usage_ai import generate_usage_ai_insights

            ai_result = generate_usage_ai_insights(config, roots_to_write, cache_dir)
            ai_status = ai_result.get("status", "unknown")
            print(f"ai insights: {ai_status} ({cache_dir / 'ai_insights.json'})")
        except Exception as exc:
            print(f"[warn] ai insights skipped: {exc}", file=sys.stderr)
    roots_text = ",".join(str(p) for p in roots_to_write)
    if args.print_env:
        print(f'export HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS="{roots_text}"')
    else:
        print(f"synced {len(roots_to_write)} HelixHarness project roots")
        print(cache_dir / "roots.txt")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
