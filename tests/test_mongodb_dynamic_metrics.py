"""Offline regression checks for MongoDB dynamic benchmark data handling.

Run directly with ``python tests/test_mongodb_dynamic_metrics.py``.  The suite
uses only fixtures and throwaway BUILD files; it deliberately never invokes
Bazel or a MongoDB benchmark executable.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from dynamic_metrics import (  # noqa: E402
    BuildIdentity,
    DynamicMetricError,
    EnvironmentIdentity,
    MetricStatus,
    MongoBenchmarkKey,
    compare_mongodb_real_time,
    discover_query_benchmark_targets,
    is_query_benchmark_target,
    parse_mongodb_benchmark_artifacts,
    parse_mongodb_benchmark_json,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mongodb_google_benchmark.json"
TARGET = "//src/mongo/db/query:query_planner_bm"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        FAILURES.append(label)


def write_json(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def main() -> int:
    print("\n[1] dynamic target discovery remains inside MongoDB query")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        query = root / "src/mongo/db/query/plan_cache"
        query.mkdir(parents=True)
        (root / "src/mongo/db/query/BUILD.bazel").write_text(
            '# mongo_cc_benchmark(name = "commented_out")\n'
            'mongo_cc_benchmark(name = "root_bm", deps = [select({"//conditions:default": []})])\n',
            encoding="utf-8",
        )
        (query / "BUILD.bazel").write_text(
            'mongo_cc_benchmark(\n  name = "nested_bm",\n  srcs = ["x_bm.cpp"],\n)\n',
            encoding="utf-8",
        )
        outside = root / "src/mongo/db/other"
        outside.mkdir(parents=True)
        (outside / "BUILD.bazel").write_text('mongo_cc_benchmark(name = "outside_bm")\n', encoding="utf-8")
        targets = discover_query_benchmark_targets(root)
        check("reads actual BUILD.bazel files dynamically", targets == tuple(sorted((
            "//src/mongo/db/query:root_bm", "//src/mongo/db/query/plan_cache:nested_bm",
        ))), str(targets))
        check("never includes outside benchmark targets", all("/other:" not in target for target in targets))

    print("\n[2] Google Benchmark aggregate JSON normalises mean real time")
    parsed = parse_mongodb_benchmark_json(
        FIXTURE, target=TARGET, build_flags=("--config=opt",), commit="before", run_id="baseline"
    )
    check("fixture is complete", parsed.complete, str(parsed.unavailable))
    check("raw/CPU values do not create metrics", len(parsed.metrics) == 1, str(parsed.metrics))
    metric = parsed.metrics[0]
    check("aggregate mean is selected", metric.key.aggregate_name == "mean")
    check("microseconds normalise to ns/op", metric.real_time_ns == 10_000.0, str(metric.real_time_ns))
    check("CV uses stddev / mean real time", abs(metric.coefficient_of_variation - 0.2) < 1e-12,
          str(metric.coefficient_of_variation))
    check("strict key retains build identity", metric.key.build.build_flags == ("--config=opt",))
    check("strict key retains environment identity", metric.key.environment.host_name == "benchmark-host")
    check("label is workload identity rather than a performance metric",
          metric.key.workload_label == "QueryClass=small Threads=1")

    print("\n[3] missing aggregate data is unavailable and fail-closed")
    with tempfile.TemporaryDirectory() as temporary:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["benchmarks"] = payload["benchmarks"][:2]
        missing = parse_mongodb_benchmark_json(write_json(Path(temporary), "missing.json", payload), target=TARGET)
        check("missing stddev has no usable metric", not missing.metrics)
        check("missing stddev is recorded", not missing.complete and "missing aggregate" in missing.unavailable[0].reason,
              str(missing.unavailable))

    print("\n[4] only exact identities compare, and ratio is candidate / baseline")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        before_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        after_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        after_payload["benchmarks"][0]["real_time"] = 12.0
        before = parse_mongodb_benchmark_json(write_json(root, "before.json", before_payload), target=TARGET)
        after = parse_mongodb_benchmark_json(write_json(root, "after.json", after_payload), target=TARGET)
        comparison = compare_mongodb_real_time(before, after)
        check("matching result is comparable", comparison.comparable)
        check("candidate/baseline ratio is reported", comparison.matched[0].ratio == 12.0 / 10.0,
              str(comparison.matched[0].ratio))
        check("both sides retain their CV", comparison.matched[0].baseline.coefficient_of_variation == 0.2
              and abs(comparison.matched[0].candidate.coefficient_of_variation - (2.0 / 12.0)) < 1e-12)

        changed_environment = json.loads(FIXTURE.read_text(encoding="utf-8"))
        changed_environment["context"]["host_name"] = "different-host"
        mismatch = compare_mongodb_real_time(
            before,
            parse_mongodb_benchmark_json(write_json(root, "environment.json", changed_environment), target=TARGET),
        )
        check("environment mismatch is explicit unmatched data", not mismatch.comparable
              and {item.status for item in mismatch.comparisons} == {MetricStatus.UNMATCHED},
              str(mismatch.comparisons))

        changed_build = json.loads(FIXTURE.read_text(encoding="utf-8"))
        changed_build["context"]["library_build_type"] = "debug"
        build_mismatch = compare_mongodb_real_time(
            before,
            parse_mongodb_benchmark_json(write_json(root, "build.json", changed_build), target=TARGET),
        )
        check("build mismatch is explicit unmatched data", not build_mismatch.comparable)

    print("\n[5] artifact-level parser preserves bad inputs as unavailable")
    artifacts = parse_mongodb_benchmark_artifacts(((FIXTURE, TARGET), ("does-not-exist.json", TARGET)))
    check("one valid artifact remains inspectable", len(artifacts.metrics) == 1)
    check("missing artifact prevents comparable success", not artifacts.complete and len(artifacts.unavailable) == 1,
          str(artifacts.unavailable))

    print("\n[6] query-scope boundary is enforced")
    try:
        parse_mongodb_benchmark_json(FIXTURE, target="//src/mongo/db:outside_query_bm")
    except DynamicMetricError:
        check("outside target rejected", True)
    else:
        check("outside target rejected", False)

    print("\n[7] query benchmark labels must be canonical")
    check("root query benchmark target accepted", is_query_benchmark_target(TARGET))
    check("nested query benchmark target accepted",
          is_query_benchmark_target("//src/mongo/db/query/plan_cache:plan_cache_key_encoding_bm"))
    check("empty package segment cannot escape query scope",
          not is_query_benchmark_target("//src/mongo/db/query//../other:outside_bm"))
    check("dot-dot package traversal cannot escape query scope",
          not is_query_benchmark_target("//src/mongo/db/query/../../other:outside_bm"))
    check("dot package segment is noncanonical",
          not is_query_benchmark_target("//src/mongo/db/query/./plan_cache:plan_cache_key_encoding_bm"))
    try:
        MongoBenchmarkKey(
            "//src/mongo/db/query/../../other:escaped_bm", "case", 1,
            "median", "", BuildIdentity("release"),
            EnvironmentIdentity("host", 1, None, None),
        )
    except DynamicMetricError:
        check("model rejects traversal target via canonical predicate", True)
    else:
        check("model rejects traversal target via canonical predicate", False)

    if FAILURES:
        print("\nFAILED:\n  - " + "\n  - ".join(FAILURES))
        return 1
    print("\nALL MongoDB dynamic metric checks PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
