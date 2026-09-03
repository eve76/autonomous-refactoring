"""Offline parsing and discovery for MongoDB query Google Benchmark artifacts.

No function in this module runs Bazel, a test binary, or ``bazel query``.  It
consumes local BUILD files and already-written JSON only.  Future orchestration
may use :func:`discover_query_benchmark_targets` to select legal targets.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    BuildIdentity,
    DynamicMetricError,
    EnvironmentIdentity,
    MongoBenchmarkKey,
    MongoParseResult,
    MongoRealTimeMetric,
    UnavailableMetric,
)


QUERY_PACKAGE = "//src/mongo/db/query"
_MACRO = "mongo_cc_benchmark"
# The dynamic gate intentionally uses one stable, native query benchmark
# rather than every benchmark macro below query/.  The selected executable
# exercises eight parameterised CanonicalQuery construction cases and emits
# Google Benchmark aggregate real_time rows without requiring a mongod
# service.  Other query benchmarks remain available to their repository
# owners, but an unrelated assertion in one of them must not make every
# refactoring candidate dynamically untestable.
APPROVED_DYNAMIC_QUERY_BENCHMARKS = (
    "//src/mongo/db/query:canonical_query_bm",
)
_NAME_RE = re.compile(r'\bname\s*=\s*"([^"\\]+)"')
_LABEL_RE = re.compile(r"(\w+)=([^\s]+)")
_TIME_TO_NS = {"ns": 1.0, "us": 1_000.0, "ms": 1_000_000.0, "s": 1_000_000_000.0}


def is_query_benchmark_target(target: str) -> bool:
    """Return whether ``target`` is a canonical label in the permitted scope.

    A prefix check is insufficient here: Bazel package paths containing empty
    segments or ``.``/``..`` can spell a label which textually starts below the
    Query package but normalises outside it.  Discovery only produces
    canonical paths, but this predicate also protects explicit runner input.
    """

    if not isinstance(target, str) or not target.startswith("//"):
        return False
    package, separator, name = target[2:].partition(":")
    if not separator or not name or ":" in name or any(character.isspace() for character in target):
        return False
    segments = package.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        return False
    canonical_query_segments = QUERY_PACKAGE[2:].split("/")
    return segments[:len(canonical_query_segments)] == canonical_query_segments


def _skip_string_or_comment(text: str, start: int) -> int:
    """Return the first index after a quoted string or line comment."""

    quote = text[start]
    if quote == "#":
        end = text.find("\n", start + 1)
        return len(text) if end == -1 else end + 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _find_call_end(text: str, opening_paren: int) -> int | None:
    """Find the matching close-paren, ignoring quoted strings and comments."""

    depth = 0
    index = opening_paren
    while index < len(text):
        char = text[index]
        if char in ("'", '"', "#"):
            index = _skip_string_or_comment(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _iter_benchmark_names(build_text: str) -> Iterator[str]:
    """Yield macro ``name`` values without matching comments or string literals."""

    index = 0
    while index < len(build_text):
        char = build_text[index]
        if char in ("'", '"', "#"):
            index = _skip_string_or_comment(build_text, index)
            continue
        if build_text.startswith(_MACRO, index):
            before = build_text[index - 1] if index else " "
            after_index = index + len(_MACRO)
            after = build_text[after_index] if after_index < len(build_text) else " "
            if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                index += 1
                continue
            opening = after_index
            while opening < len(build_text) and build_text[opening].isspace():
                opening += 1
            if opening >= len(build_text) or build_text[opening] != "(":
                index += 1
                continue
            close = _find_call_end(build_text, opening)
            if close is None:
                index = opening + 1
                continue
            name = _NAME_RE.search(build_text[opening + 1 : close])
            if name:
                yield name.group(1)
            index = close + 1
            continue
        index += 1


def discover_query_benchmark_targets(mongo_root: str | Path) -> tuple[str, ...]:
    """Discover ``mongo_cc_benchmark`` targets below ``src/mongo/db/query``.

    The discovery follows the local performance guide: it reads all actual
    ``BUILD.bazel`` files at runtime and never keeps a manually copied target
    list.  A repository lacking the query directory, a malformed target, or an
    empty result is an error because an empty benchmark selection is unsafe.
    """

    root = Path(mongo_root)
    query_root = root / "src" / "mongo" / "db" / "query"
    if not query_root.is_dir():
        raise DynamicMetricError(f"MongoDB query directory does not exist: {query_root}")

    targets: set[str] = set()
    for build_file in sorted(query_root.rglob("BUILD.bazel")):
        package_path = build_file.parent.relative_to(root).as_posix()
        for name in _iter_benchmark_names(build_file.read_text(encoding="utf-8", errors="ignore")):
            target = f"//{package_path}:{name}"
            if not is_query_benchmark_target(target):  # Defensive invariant.
                raise DynamicMetricError(f"discovery escaped query scope: {target}")
            targets.add(target)
    if not targets:
        raise DynamicMetricError(f"no mongo_cc_benchmark targets found below {query_root}")
    return tuple(sorted(targets))


def approved_query_dynamic_benchmark_targets(
    discovered: Iterable[str],
) -> tuple[str, ...]:
    """Return the vetted native MongoDB benchmarks used by the dynamic gate.

    ``discovered`` must come from :func:`discover_query_benchmark_targets` so
    a checkout missing the approved executable fails closed instead of
    silently selecting a similarly named target or expanding the scope.
    """

    available = set(discovered)
    missing = [target for target in APPROVED_DYNAMIC_QUERY_BENCHMARKS if target not in available]
    if missing:
        raise DynamicMetricError(
            "approved MongoDB dynamic benchmark target is unavailable: "
            + ", ".join(missing)
        )
    return APPROVED_DYNAMIC_QUERY_BENCHMARKS


def _require_string(mapping: dict[str, Any], field: str, context: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise DynamicMetricError(f"{context}: required string {field!r} is missing")
    return value


def _require_positive_int(mapping: dict[str, Any], field: str, context: str) -> int:
    value = mapping.get(field)
    if isinstance(value, bool):
        raise DynamicMetricError(f"{context}: {field!r} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DynamicMetricError(f"{context}: {field!r} must be a positive integer") from exc
    if parsed <= 0:
        raise DynamicMetricError(f"{context}: {field!r} must be a positive integer")
    return parsed


def _time_in_ns(value: Any, unit: Any, context: str) -> float:
    if isinstance(value, bool):
        raise DynamicMetricError(f"{context}: real_time must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise DynamicMetricError(f"{context}: real_time must be numeric") from exc
    if numeric < 0:
        raise DynamicMetricError(f"{context}: real_time must not be negative")
    normalized_unit = str(unit).lower()
    if normalized_unit not in _TIME_TO_NS:
        raise DynamicMetricError(f"{context}: unsupported time_unit {unit!r}")
    return numeric * _TIME_TO_NS[normalized_unit]


def _environment_from_context(context: dict[str, Any]) -> EnvironmentIdentity:
    context_name = "Google Benchmark context"
    host = _require_string(context, "host_name", context_name)
    cpus = _require_positive_int(context, "num_cpus", context_name)
    mhz = context.get("mhz_per_cpu")
    if mhz is not None:
        try:
            mhz = float(mhz)
        except (TypeError, ValueError) as exc:
            raise DynamicMetricError("Google Benchmark context: mhz_per_cpu must be numeric") from exc
    scaling = context.get("cpu_scaling_enabled")
    if scaling is not None and not isinstance(scaling, bool):
        raise DynamicMetricError("Google Benchmark context: cpu_scaling_enabled must be boolean")
    return EnvironmentIdentity(host, cpus, mhz, scaling)


def _case_hint(row: dict[str, Any]) -> str:
    return str(row.get("run_name") or row.get("name") or "unknown-case")


def _parse_label(row: dict[str, Any]) -> str:
    """Canonicalise whitespace so equivalent labels obtain the same key."""

    label = row.get("label", "")
    if label is None:
        return ""
    if not isinstance(label, str):
        raise DynamicMetricError(f"{_case_hint(row)}: label must be a string")
    # Preserve unknown label fields, but normalise spacing.  The mapping is
    # intentionally not used as a metric; it is workload identity only.
    parsed = _LABEL_RE.findall(label)
    return " ".join(f"{key}={value}" for key, value in parsed) if parsed else " ".join(label.split())


def parse_mongodb_benchmark_json(
    json_path: str | Path,
    *,
    target: str,
    build_flags: Iterable[str] = (),
    commit: str = "",
    run_id: str = "",
) -> MongoParseResult:
    """Parse an aggregate-only Google Benchmark JSON artifact.

    Exactly one ``mean``, ``median``, and ``stddev`` row is required for each
    logical workload.  The emitted metric is mean ``real_time`` across the
    configured repetitions; median and stddev are retained as validation
    evidence and stddev/mean supplies CV. Any malformed group is
    recorded as unavailable rather than partially accepted.
    """

    if not is_query_benchmark_target(target):
        raise DynamicMetricError(f"target is outside MongoDB query scope: {target}")
    source = Path(json_path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DynamicMetricError(f"cannot parse benchmark JSON {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DynamicMetricError(f"benchmark JSON {source} must contain an object")
    context = raw.get("context")
    rows = raw.get("benchmarks")
    if not isinstance(context, dict) or not isinstance(rows, list):
        raise DynamicMetricError(f"benchmark JSON {source} requires object context and list benchmarks")

    environment = _environment_from_context(context)
    build = BuildIdentity(_require_string(context, "library_build_type", "Google Benchmark context"),
                          tuple(str(flag) for flag in build_flags))
    grouped: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    unavailable: list[UnavailableMetric] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            unavailable.append(UnavailableMetric(target, f"row-{row_index}", str(source), "benchmark row is not an object"))
            continue
        if row.get("run_type") != "aggregate":
            continue
        aggregate = row.get("aggregate_name")
        if aggregate not in {"mean", "median", "stddev"}:
            continue
        try:
            run_name = _require_string(row, "run_name", f"benchmark row {row_index}")
            threads = _require_positive_int(row, "threads", f"benchmark row {row_index}")
            label = _parse_label(row)
            group_key = (run_name, threads, label)
            if aggregate in grouped[group_key]:
                raise DynamicMetricError(f"duplicate aggregate row {aggregate!r}")
            grouped[group_key][aggregate] = row
        except DynamicMetricError as exc:
            unavailable.append(UnavailableMetric(target, _case_hint(row), str(source), str(exc)))

    if not grouped and not unavailable:
        unavailable.append(UnavailableMetric(target, "all-cases", str(source), "no aggregate benchmark rows found"))

    metrics: list[MongoRealTimeMetric] = []
    for (run_name, threads, label), aggregates in sorted(grouped.items()):
        missing = sorted({"mean", "median", "stddev"}.difference(aggregates))
        if missing:
            unavailable.append(UnavailableMetric(
                target, run_name, str(source), f"missing aggregate row(s): {', '.join(missing)}"
            ))
            continue
        try:
            median = aggregates["median"]
            mean = aggregates["mean"]
            stddev = aggregates["stddev"]
            mean_ns = _time_in_ns(mean.get("real_time"), mean.get("time_unit"), run_name)
            stddev_ns = _time_in_ns(stddev.get("real_time"), stddev.get("time_unit"), run_name)
            if mean_ns <= 0:
                raise DynamicMetricError(f"{run_name}: mean real_time must be positive for CV")
            # Require the median row to be numerically well-formed as part of
            # the complete seven-repetition aggregate, even though the gate
            # compares the requested arithmetic mean.
            _time_in_ns(median.get("real_time"), median.get("time_unit"), run_name)
            iterations = _require_positive_int(mean, "iterations", run_name)
            key = MongoBenchmarkKey(target, run_name, threads, "mean", label, build, environment)
            metrics.append(MongoRealTimeMetric(
                key=key,
                real_time_ns=mean_ns,
                mean_real_time_ns=mean_ns,
                stddev_real_time_ns=stddev_ns,
                coefficient_of_variation=stddev_ns / mean_ns,
                iterations=iterations,
                source_json=str(source),
                date=str(context.get("date", "")),
                executable=str(context.get("executable", "")),
                commit=commit,
                run_id=run_id,
            ))
        except DynamicMetricError as exc:
            unavailable.append(UnavailableMetric(target, run_name, str(source), str(exc)))
    return MongoParseResult(tuple(metrics), tuple(unavailable))


def parse_mongodb_benchmark_artifacts(
    artifacts: Iterable[tuple[str | Path, str]],
    *,
    build_flags: Iterable[str] = (),
    commit: str = "",
    run_id: str = "",
) -> MongoParseResult:
    """Combine already-written JSON artifacts without hiding any parse failure."""

    all_metrics: list[MongoRealTimeMetric] = []
    all_unavailable: list[UnavailableMetric] = []
    seen_keys: set[MongoBenchmarkKey] = set()
    for path, target in artifacts:
        try:
            parsed = parse_mongodb_benchmark_json(
                path, target=target, build_flags=build_flags, commit=commit, run_id=run_id
            )
        except DynamicMetricError as exc:
            all_unavailable.append(UnavailableMetric(target, "artifact", str(path), str(exc)))
            continue
        for metric in parsed.metrics:
            if metric.key in seen_keys:
                all_unavailable.append(UnavailableMetric(
                    metric.key.target, metric.key.run_name, metric.source_json,
                    "duplicate normalized metric key across artifacts",
                ))
                continue
            seen_keys.add(metric.key)
            all_metrics.append(metric)
        all_unavailable.extend(parsed.unavailable)
    return MongoParseResult(tuple(all_metrics), tuple(all_unavailable))
