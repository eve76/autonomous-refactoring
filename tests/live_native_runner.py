#!/usr/bin/env python3
"""Count and run the focused native commands for the paid live smoke test."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


MONGO_TESTS = (
    "ExtractAllElementsAlongPath",
    "ExtractElementAtPathOrArrayAlongPath",
)
FERRET_TESTS = ("TestState", "TestMakeReport")


def _load_counts(path: Path) -> dict:
    if not path.exists():
        return {"build_commands": 0, "test_commands": 0, "commands": []}
    return json.loads(path.read_text())


def _record(path: Path, kind: str, command: list[str]) -> None:
    counts = _load_counts(path)
    counts[f"{kind}_commands"] += 1
    counts["commands"].append({"kind": kind, "argv": command})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(counts, indent=2))


def _run(command: list[str]) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command).returncode


def mongo(action: str, root: Path, counts: Path) -> int:
    bazel = shutil.which("bazel")
    if not bazel:
        raise SystemExit("Bazel/Bazelisk not found")
    startup = [
        str(Path(bazel).resolve()),
        f"--output_user_root={root / 'bazel-user-root'}",
        f"--output_base={root / 'bazel-output'}",
    ]
    target = "//src/mongo/db/query/bson:multikey_db_bson_test"
    if action == "build":
        command = startup + [
            "build", "--config=local",
            "//src/mongo/db/query/bson:multikey_dotted_path_support",
            target,
        ]
        _record(counts, "build", command)
        return _run(command)

    failed = False
    for suite in MONGO_TESTS:
        command = startup + [
            "test", "--config=local", "--test_output=errors", target,
            f"--test_arg=--suite={suite}",
        ]
        _record(counts, "test", command)
        failed = _run(command) != 0 or failed
    return int(failed)


def ferret(action: str, root: Path, counts: Path) -> int:
    binary = root / "ferret-telemetry.test"
    if action == "build":
        command = [
            "go", "test", "-c", "-tags=ferretdb_dev",
            "-o", str(binary), "./internal/util/telemetry",
        ]
        _record(counts, "build", command)
        return _run(command)

    failed = False
    for name in FERRET_TESTS:
        command = [str(binary), f"-test.run=^{name}$", "-test.count=1"]
        _record(counts, "test", command)
        failed = _run(command) != 0 or failed
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "test"))
    parser.add_argument("--scenario", choices=("mongo", "ferret"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--counts", type=Path, required=True)
    args = parser.parse_args()
    args.run_root.mkdir(parents=True, exist_ok=True)
    if args.scenario == "mongo":
        return mongo(args.action, args.run_root, args.counts)
    return ferret(args.action, args.run_root, args.counts)


if __name__ == "__main__":
    raise SystemExit(main())
