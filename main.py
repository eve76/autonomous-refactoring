"""Entry point for the multi-agent refactoring system."""

import argparse
from pathlib import Path

from config import Config
from coordination.coordinator import Coordinator


def parse_args() -> Config:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, type=Path, help="Path to target git repository")
    p.add_argument("--subdir", default=".", help="Subdirectory under repo to refactor")
    p.add_argument("--work-root", required=True, type=Path, help="Directory to host worktrees")
    p.add_argument("--analysts", type=int, default=3)
    p.add_argument("--programmers", type=int, default=3)
    args = p.parse_args()

    return Config(
        repo_root=args.repo.resolve(),
        target_subdir=args.subdir,
        work_root=args.work_root.resolve(),
        num_analysts=args.analysts,
        num_programmers=args.programmers,
    )


def main() -> None:
    cfg = parse_args()
    coordinator = Coordinator(cfg)
    coordinator.run()


if __name__ == "__main__":
    main()
