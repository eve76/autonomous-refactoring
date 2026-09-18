#!/usr/bin/env python3
"""Repository-native build/test commands used by production profiles."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess


BASELINE_OUTPUT_BASE_ENV = "EXPERIMENT_BAZEL_OUTPUT_BASE"


def _require_checkout(profile: str, root: Path) -> None:
    required = (
        ("go.mod", "Taskfile.yml")
        if profile == "ferretdb"
        else ("MODULE.bazel", "src/mongo/db/query/BUILD.bazel")
    )
    missing = [value for value in required if not (root / value).exists()]
    if missing:
        raise SystemExit(
            f"{profile} validation must run from its repository root; "
            f"missing {', '.join(missing)}"
        )


def _command(profile: str, action: str) -> list[str]:
    if profile == "ferretdb":
        go = shutil.which("go")
        if not go:
            raise SystemExit("Go toolchain not found")
        common = [
            str(Path(go).resolve()), "-race", "-tags=ferretdb_dev", "./...",
        ]
        if action == "build":
            return [common[0], "test", "-run=^$", *common[1:]]
        return [
            common[0], "test", "-short", "-count=1", "-shuffle=on",
            "-timeout=35m", *common[1:],
        ]

    bazel = shutil.which("bazel")
    if not bazel:
        raise SystemExit("Bazel/Bazelisk not found")
    target = "//src/mongo/db/query/..."
    disk_cache = (
        Path(__file__).resolve().parent.parent
        / "production_runs" / "mongodb-query" / "bazel_disk_cache"
    )
    disk_cache.mkdir(parents=True, exist_ok=True)
    common = [
        "--config=local",
        f"--disk_cache={disk_cache}",
        "--experimental_disk_cache_gc_max_size=150G",
        "--experimental_disk_cache_gc_idle_delay=1m",
    ]
    startup = [str(Path(bazel).resolve())]
    output_base = os.environ.get(BASELINE_OUTPUT_BASE_ENV, "").strip()
    if output_base:
        output_base_path = Path(output_base)
        if not output_base_path.is_absolute():
            raise SystemExit(f"{BASELINE_OUTPUT_BASE_ENV} must be absolute")
        startup.append(f"--output_base={output_base_path}")
    if action == "build":
        return [*startup, "build", *common, target]
    return [
        *startup, "test", *common,
        "--test_output=errors", "--keep_going",
        "--test_tag_filters="
        "-mongo_integration_test,-mongo_integration_test_debug,"
        "-mongo_benchmark,-mongo_benchmark_debug,-intermediate_debug",
        target,
    ]


def _commands(profile: str, action: str) -> list[list[str]]:
    """Return the complete validation sequence for one profile action."""
    commands: list[list[str]] = []
    if profile == "ferretdb":
        go = shutil.which("go")
        if not go:
            raise SystemExit("Go toolchain not found")
        # FerretDB's version package tests require the ignored files produced
        # by this repository-native generator. Direct `go test` leaves
        # Version/Commit/Branch as "unknown" and makes every valid patch fail.
        commands.append([
            str(Path(go).resolve()), "generate", "./build/version",
        ])
    commands.append(_command(profile, action))
    return commands


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=("ferretdb", "mongodb-query"))
    parser.add_argument("action", choices=("build", "test"))
    args = parser.parse_args()
    root = Path.cwd()
    _require_checkout(args.profile, root)
    output_base = os.environ.get(BASELINE_OUTPUT_BASE_ENV, "").strip()
    try:
        for command in _commands(args.profile, args.action):
            print("+ " + " ".join(command), flush=True)
            result = subprocess.run(command, cwd=str(root))
            if result.returncode:
                return result.returncode
        return 0
    finally:
        # A dedicated output base starts its own Bazel server. Shut it down so
        # the coordinator can remove the ephemeral directory without leaving
        # a server holding deleted files open. Normal gate builds do not set
        # this variable and therefore retain their usual incremental state.
        if args.profile == "mongodb-query" and output_base:
            bazel = shutil.which("bazel")
            if bazel:
                subprocess.run(
                    [
                        str(Path(bazel).resolve()),
                        f"--output_base={Path(output_base)}",
                        "shutdown",
                    ],
                    cwd=str(root),
                    check=False,
                )


if __name__ == "__main__":
    raise SystemExit(main())
