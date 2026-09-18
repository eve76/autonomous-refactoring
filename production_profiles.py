"""Locked production profiles for the two experiment repositories.

Profiles define measurement scope and repository-native validation together so
an unattended run cannot accidentally widen MongoDB beyond query/ or fall back
to no-op build/test commands.
"""

from dataclasses import dataclass
from pathlib import Path
import shutil
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent.parent
PROFILE_NAMES = ("ferretdb", "mongodb-query")


def project_tool(name: str) -> str:
    candidate = Path(sys.executable).parent / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    return str(Path(resolved).resolve()) if resolved else name


@dataclass(frozen=True)
class ProductionProfile:
    name: str
    repo_root: Path
    work_root: Path
    target_subdir: str
    language: str
    sparse_worktrees: bool
    build_cmd: tuple[str, ...]
    test_cmd: tuple[str, ...]
    prewarm_build_cache: bool
    build_timeout_sec: int
    test_timeout_sec: int
    gate_timeout_sec: int
    lizard_binary: str
    gocognit_binary: str
    duplo_binary: str
    dupl_binary: str = ""
    dupl_threshold_tokens: int = 100
    duplo_min_block_lines: int = 4
    gate_allowed_untracked_paths: tuple[str, ...] = ()
    repo_allowed_untracked_paths: tuple[str, ...] = ()
    baseline_ref: str = "HEAD"


def get_production_profile(name: str) -> ProductionProfile:
    if name not in PROFILE_NAMES:
        raise ValueError(
            f"unknown production profile {name!r}; choose one of "
            f"{', '.join(PROFILE_NAMES)}"
        )
    validator = PROJECT_ROOT / "scripts" / "production_validation.py"
    common = (str(Path(sys.executable).absolute()), str(validator), name)
    if name == "ferretdb":
        return ProductionProfile(
            name=name,
            baseline_ref="39afbdcafe3f00fc029e0e2b704640970bed8b4b",
            repo_root=(WORKSPACE_ROOT / "ferret-dev" / "FerretDB").resolve(),
            work_root=(PROJECT_ROOT / "production_runs" / name).resolve(),
            target_subdir=".",
            language="go",
            sparse_worktrees=False,
            build_cmd=(*common, "build"),
            test_cmd=(*common, "test"),
            prewarm_build_cache=False,
            build_timeout_sec=45 * 60,
            test_timeout_sec=45 * 60,
            # 45m build + 45m correctness + 60m dynamic benchmark +
            # Config's five-minute static-analysis margin.
            gate_timeout_sec=3 * 60 * 60,
            lizard_binary=project_tool("lizard"),
            gocognit_binary=project_tool("gocognit"),
            duplo_binary="",
            dupl_binary=project_tool("dupl"),
            dupl_threshold_tokens=100,
            repo_allowed_untracked_paths=(
                ".env.local",
                ".local-cache",
                "env-setup.log",
                "integration-build.log",
                "integration-postgresql-serial.log",
                "integration-postgresql.log",
                "newpool.log",
                "scripts",
                "task-test.log",
                "test-unit.log",
            ),
        )
    return ProductionProfile(
        name=name,
        baseline_ref="fbb28cf8c44023d334a646fe496fb95d355dc6f0",
        repo_root=(WORKSPACE_ROOT / "dev" / "mongo").resolve(),
        work_root=(PROJECT_ROOT / "production_runs" / name).resolve(),
        target_subdir="src/mongo/db/query",
        language="cpp",
        # Query sources depend on the rest of MongoDB's Bazel graph. The
        # measurement and edit authorization remain locked to query/.
        sparse_worktrees=False,
        build_cmd=(*common, "build"),
        test_cmd=(*common, "test"),
        # Build the complete Query target set once on the immutable baseline
        # before any paid agent is dispatched.  All feature worktrees use the
        # same --disk_cache path, so unchanged actions are reusable there.
        prewarm_build_cache=True,
        build_timeout_sec=2 * 60 * 60,
        test_timeout_sec=4 * 60 * 60,
        # 2h build + 4h correctness + 60m dynamic benchmark + static margin.
        gate_timeout_sec=7 * 60 * 60 + 15 * 60,
        lizard_binary=project_tool("lizard"),
        gocognit_binary=project_tool("gocognit"),
        duplo_binary=str((PROJECT_ROOT / "bin" / "duplo").resolve()),
        dupl_binary="",
        duplo_min_block_lines=4,
        gate_allowed_untracked_paths=("MODULE.bazel.lock",),
        repo_allowed_untracked_paths=(
            ".venv", "experiment_logs", "MODULE.bazel.lock",
            "activate_mongo_env.sh",
        ),
    )
