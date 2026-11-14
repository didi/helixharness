#!/usr/bin/env python3
"""Warning-only spec/evidence checks; independent of shell and working directory."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
ACTIVE = ROOT / "docs/specs/active"


def main():
    print("=== HelixHarness spec-sync check ===")
    features = sorted(path for path in ACTIVE.iterdir() if path.is_dir()) if ACTIVE.is_dir() else []
    for feature in features:
        spec = feature / "spec.md"
        if not spec.is_file():
            print(f"WARNING {feature.name}: spec.md missing")
        if (ROOT / "helixharness.workspace.yaml").is_file():
            continue
        print(f"{feature.name}: tasks.md {'exists' if (feature / 'tasks.md').is_file() else 'missing'}")
        text = spec.read_text(encoding="utf-8") if spec.is_file() else ""
        frontmatter = text.split("---", 2)[1] if text.startswith("---\n") and text.count("---") >= 2 else ""
        match = re.search(r"^status:\s*([^\n#]+)", frontmatter, re.M)
        status = match.group(1).strip().strip("\"'") if match else ""
        report = feature / "verify-report.md"
        if status in {"implemented", "completed"} and (not report.is_file() or report.stat().st_size == 0):
            print(f"WARNING {feature.name}: verify-report.md missing or empty")
    if not features:
        print("No active features")
    print("=== spec-sync check complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
