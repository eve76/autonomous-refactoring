"""Run every verification suite. Exits non-zero if any check fails.

    python tests/run_all.py

These suites check the behaviours the thesis specifies, using a
throwaway git repository and stubbed LLM calls, so they need neither an
API key nor a target codebase.
"""

import subprocess
import sys
from pathlib import Path

SUITES = [
    ("merge gate (end-to-end on a real git repo)", "test_merge_gate.py"),
    ("per-run branch isolation (thesis §4.4)", "test_run_isolation.py"),
    ("per-metric distribution statistics (Tables 4.1 / 5.1)", "test_metric_stats.py"),
    ("reproduction artefacts (history, plot, state, summary)", "test_artifacts.py"),
    ("two-tier stuck-agent policy", "test_stuck_policy.py"),
    ("gate failure counts (§5.1 / §5.4)", "test_failure_counts.py"),
    ("issue attempt history and dispatch limits", "test_issue_attempt_policy.py"),
    ("Anthropic / DeepSeek provider configuration", "test_provider_config.py"),
    ("provider-neutral token usage accounting", "test_token_usage.py"),
    ("Go cognitive complexity via gocognit", "test_go_cognitive.py"),
    ("conservative token-saving stages 2 and 3", "test_token_optimizations.py"),
    ("MongoDB offline dynamic benchmark metrics", "test_mongodb_dynamic_metrics.py"),
    ("FerretDB offline dynamic benchmark metrics", "test_ferretdb_dynamic_metrics.py"),
    ("dynamic gate modes and safe benchmark artifacts", "test_dynamic_gate.py"),
]


def main() -> int:
    here = Path(__file__).resolve().parent
    failed = []
    for label, script in SUITES:
        print(f"\n{'=' * 62}\n### {label}\n{'=' * 62}")
        rc = subprocess.run([sys.executable, str(here / script)]).returncode
        if rc != 0:
            failed.append(label)

    print(f"\n{'=' * 62}")
    if failed:
        print("SUITES FAILED:\n  - " + "\n  - ".join(failed))
        return 1
    print(f"ALL {len(SUITES)} SUITES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
