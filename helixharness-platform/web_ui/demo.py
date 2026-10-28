"""Launch a local demo from synthetic source files, without external jobs."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path


def configure_demo(data_dir: Path) -> Path:
    """Prepare isolated demo databases and editable configuration."""
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "bench/evaluation.db"
    from bench.demo import seed_demo as seed_benchmark

    seed_benchmark(database)
    database.chmod(0o600)
    databases = {
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH": database,
        "HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH": data_dir / "eval/feedback.db",
        "HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH": data_dir / "eval/telemetry.db",
        "HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH": data_dir / "eval/stage_supplements.db",
        "HELIXHARNESS_PLATFORM_USAGE_FIRST_SEEN_DB_PATH": data_dir / "eval/first_seen.db",
        "HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH": data_dir / "evolution/evolution.db",
    }
    for key, path in databases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    catalog_path = data_dir / "evolution/capabilities.yaml"
    if not catalog_path.exists():
        source_catalog = Path(__file__).resolve().parents[1] / "evolution/catalog/capabilities.yaml"
        catalog_path.write_bytes(source_catalog.read_bytes())
    os.environ["HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH"] = str(catalog_path)
    os.environ["HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE"] = str(data_dir / "eval")
    os.environ["HELIXHARNESS_PLATFORM_USAGE_SYNC_CONFIG"] = str(data_dir / "eval/usage_repos.yaml")
    from eval.demo import seed_demo

    roots = data_dir / "demo-project"
    if not (roots / "docs/eval/example-feature/eval-log.json").exists():
        seed_demo(roots, databases["HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH"])
    os.environ["HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS"] = str(roots)
    os.environ["HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE"] = "demo"
    os.environ["HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED"] = "false"
    os.environ["HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED"] = "false"
    os.environ["HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH"] = str(data_dir / "platform_config.json")
    return database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="helixharness-demo-"))
    configure_demo(data_dir.resolve())
    import uvicorn

    from web_ui.main import create_app

    app = create_app()
    app.state.demo_mode = True
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
