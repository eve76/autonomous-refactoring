"""CLI entry the programmer agent invokes from inside its worktree.

Usage (run from inside the worktree):
    python /path/to/experiment/merge_gate/cli.py

The gate re-measures main's penalty itself; no caller-supplied value
is needed. Reads .gate_config.json (written by the coordinator at init
time) for repo_root / target_subdir / thresholds / weights / build &
test commands / reverted_dir. Prints a JSON result line on stdout.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


def _load_config(worktree: Path) -> dict:
    cfg_path = worktree / ".gate_config.json"
    if not cfg_path.exists():
        print(json.dumps({"success": False, "reason": "missing .gate_config.json"}))
        sys.exit(2)
    return json.loads(cfg_path.read_text())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--worktree-idx", type=int, default=0,
        help="Programmer index used in the gate's commit message",
    )
    args = p.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from merge_gate.gate import MergeGate

    worktree = Path.cwd()
    cfg = _load_config(worktree)

    reverted_dir = cfg.get("reverted_dir")
    reverted_dir_path = Path(reverted_dir) if reverted_dir else None

    gate = MergeGate(
        worktree=worktree,
        repo_root=Path(cfg["repo_root"]),
        target_subdir=cfg["target_subdir"],
        thresholds=cfg["thresholds"],
        build_cmd=cfg["build_cmd"],
        test_cmd=cfg["test_cmd"],
        main_branch=cfg.get("main_branch", "main"),
        duplo_binary=cfg.get("duplo_binary", ""),
        duplo_min_block_lines=int(cfg.get("duplo_min_block_lines", 10)),
        lizard_binary=cfg.get("lizard_binary", "lizard"),
        lizard_language=cfg.get("lizard_language", "cpp"),
        weights=cfg.get("weights"),
        reverted_dir=reverted_dir_path,
        worktree_idx=args.worktree_idx,
    )
    result = gate.run()
    print(json.dumps(asdict(result)))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
