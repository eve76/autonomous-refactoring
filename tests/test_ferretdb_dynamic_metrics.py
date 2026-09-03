"""Offline tests for native FerretDB Go benchmark metric handling.

Run with ``python tests/test_ferretdb_dynamic_metrics.py``.  This suite only
parses fixture text and builds a redacted command template: it never executes
``go test`` and never starts or contacts PostgreSQL.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from dynamic_metrics import (  # noqa: E402
    DEFAULT_POSTGRESQL_URL_ENV_VAR,
    DynamicMetricError,
    FerretDBBenchmarkConfig,
    MetricStatus,
    build_ferretdb_benchmark_command,
    compare_ferretdb_metrics,
    ferretdb_benchmark_availability,
    parse_ferretdb_benchmark_text,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ferretdb_go_benchmark.txt"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    text = FIXTURE.read_text(encoding="utf-8")

    print("\n[1] only native Go ns/op and B/op are retained")
    parsed = parse_ferretdb_benchmark_text(text, source_output="fixture.txt", run_id="baseline")
    check("fixture is complete", parsed.complete, str(parsed.unavailable))
    check("every Go -count output line stays a raw sample", len(parsed.metrics) == 6)
    find = parsed.metrics[0]
    check("ns/op is normalized directly from Go output", find.ns_per_op == 100.0)
    check("B/op is normalized directly from -benchmem output", find.bytes_per_op == 128.0)
    check("allocs/op is not a stored or penalty metric", not hasattr(find, "allocs_per_op"))
    check("docs-returned is workload identity only", find.key.docs_returned == 1.0)
    check("case name drops only Go CPU suffix", find.key.case_name == "BenchmarkFind/ferretdb/Int32IDIndex")
    check("insert workload has no invented docs-returned value", parsed.metrics[3].key.docs_returned is None)

    print("\n[2] missing -benchmem allocation data fails closed")
    no_memory = parse_ferretdb_benchmark_text(
        "BenchmarkFind/ferretdb/Int32IDIndex-8  1000  100 ns/op  1 docs-returned\n",
        source_output="missing-benchmem.txt",
    )
    check("line missing B/op is unavailable", not no_memory.complete and not no_memory.metrics,
          str(no_memory.unavailable))
    check("failure names missing B/op", "B/op" in no_memory.unavailable[0].reason)

    print("\n[3] baseline/candidate comparison uses matching native samples")
    candidate_text = (text
        .replace("100.0 ns/op  128 B/op", "120.0 ns/op  160 B/op")
        .replace("120.0 ns/op  128 B/op", "144.0 ns/op  160 B/op")
        .replace("110.0 ns/op  128 B/op", "132.0 ns/op  160 B/op")
        .replace("400.0 ns/op  256 B/op", "440.0 ns/op  256 B/op")
        .replace("420.0 ns/op  256 B/op", "462.0 ns/op  256 B/op")
        .replace("410.0 ns/op  256 B/op", "451.0 ns/op  256 B/op"))
    comparison = compare_ferretdb_metrics(
        parsed,
        parse_ferretdb_benchmark_text(candidate_text, source_output="candidate.txt", run_id="candidate"),
    )
    check("same case identities are comparable", comparison.comparable, str(comparison.unavailable))
    check("both Find and Insert cases are retained", len(comparison.matched) == 2)
    find_comparison = next(case for case in comparison.matched if case.key.case_name.startswith("BenchmarkFind"))
    check("time ratio is candidate mean / baseline mean", find_comparison.ns_per_op_ratio == 1.2,
          str(find_comparison.ns_per_op_ratio))
    check("memory ratio is candidate mean / baseline mean", find_comparison.bytes_per_op_ratio == 1.25,
          str(find_comparison.bytes_per_op_ratio))
    check("all repeat samples remain represented", len(find_comparison.baseline_samples) == 3)

    print("\n[4] workload and repetition mismatches cannot be treated as performance results")
    docs_changed = parse_ferretdb_benchmark_text(
        candidate_text.replace("1 docs-returned", "2 docs-returned"), source_output="docs-changed.txt"
    )
    docs_comparison = compare_ferretdb_metrics(parsed, docs_changed)
    check("docs-returned change is an explicit unmatched workload", not docs_comparison.comparable
          and {case.status for case in docs_comparison.comparisons} == {MetricStatus.AVAILABLE, MetricStatus.UNMATCHED},
          str(docs_comparison.comparisons))
    fewer_samples = parse_ferretdb_benchmark_text(
        "\n".join(candidate_text.splitlines()[:-2]) + "\n", source_output="fewer-samples.txt"
    )
    count_comparison = compare_ferretdb_metrics(parsed, fewer_samples)
    check("different repeat count is unavailable", not count_comparison.comparable
          and any(case.status is MetricStatus.UNAVAILABLE for case in count_comparison.comparisons),
          str(count_comparison.comparisons))

    print("\n[5] external PostgreSQL configuration is explicit and redacted")
    default_availability = ferretdb_benchmark_availability(FerretDBBenchmarkConfig(), environ={})
    check("default policy is disabled", not default_availability.available)
    missing_url = ferretdb_benchmark_availability(FerretDBBenchmarkConfig(enabled=True), environ={})
    check("enabled policy without URL env is unavailable", not missing_url.available
          and missing_url.postgresql_url_env_var == DEFAULT_POSTGRESQL_URL_ENV_VAR)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "integration").mkdir()
        secret_url = "postgres://username:secret-password@localhost:5432/benchmark"
        command = build_ferretdb_benchmark_command(
            root,
            FerretDBBenchmarkConfig(enabled=True, repetitions=5),
            environ={DEFAULT_POSTGRESQL_URL_ENV_VAR: secret_url},
        )
        command_text = " ".join(command.argv_template)
        check(
            "command runs inside the independent integration Go module",
            Path(command.working_directory).name == "integration"
            and "." in command.argv_template,
        )
        check("command excludes correctness tests", "-run=^$" in command.argv_template)
        check("command supports linked worktrees without parent VCS stamping",
              "-buildvcs=false" in command.argv_template)
        check("command includes native time and memory flags", "-benchmem" in command.argv_template
              and "-count=5" in command.argv_template)
        check("command selects only existing Find/Insert benchmarks", "-bench=^(BenchmarkFind|BenchmarkInsert)$" in command.argv_template)
        check("command uses FerretDB development backend flags", "-tags=ferretdb_dev" in command.argv_template
              and "-target-backend=ferretdb" in command.argv_template)
        check("command disables OTLP output that can corrupt benchmark lines",
              "-otel-traces-url=" in command.argv_template)
        check("URL is an environment-name placeholder, never a value", secret_url not in command_text
              and "${FERRETDB_BENCHMARK_POSTGRESQL_URL}" in command_text)
        check("availability result never exposes URL value", secret_url not in repr(
            ferretdb_benchmark_availability(FerretDBBenchmarkConfig(enabled=True),
                                             environ={DEFAULT_POSTGRESQL_URL_ENV_VAR: secret_url})
        ))

    try:
        FerretDBBenchmarkConfig(enabled=True, benchmarks=("BenchmarkOther",))
    except DynamicMetricError:
        check("unknown benchmark selection is rejected", True)
    else:
        check("unknown benchmark selection is rejected", False)

    if FAILURES:
        print("\nFAILED:\n  - " + "\n  - ".join(FAILURES))
        return 1
    print("\nALL FerretDB dynamic metric checks PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
