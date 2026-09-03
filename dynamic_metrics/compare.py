"""Strict, fail-closed comparison of normalized MongoDB real-time metrics."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Iterable

from .models import (
    MetricStatus,
    MongoBenchmarkKey,
    MongoCaseComparison,
    MongoComparisonResult,
    MongoParseResult,
    MongoRealTimeMetric,
    UnavailableMetric,
)
from .models import (
    FerretDBBenchmarkKey,
    FerretDBBenchmarkMetric,
    FerretDBCaseComparison,
    FerretDBComparisonResult,
    FerretDBParseResult,
)


def _index_metrics(
    metrics: Iterable[MongoRealTimeMetric],
    side: str,
) -> tuple[dict[MongoBenchmarkKey, MongoRealTimeMetric], list[UnavailableMetric]]:
    """Index a side and turn duplicate identities into explicit failures."""

    grouped: dict[MongoBenchmarkKey, list[MongoRealTimeMetric]] = defaultdict(list)
    for metric in metrics:
        grouped[metric.key].append(metric)
    unique: dict[MongoBenchmarkKey, MongoRealTimeMetric] = {}
    unavailable: list[UnavailableMetric] = []
    for key, same_key in grouped.items():
        if len(same_key) == 1:
            unique[key] = same_key[0]
            continue
        unavailable.append(UnavailableMetric(
            key.target, key.run_name, same_key[0].source_json,
            f"{side} has {len(same_key)} metrics with the same strict comparison key",
        ))
    return unique, unavailable


def compare_mongodb_real_time(
    baseline: MongoParseResult,
    candidate: MongoParseResult,
) -> MongoComparisonResult:
    """Compare only same-key mean real-time metrics.

    The ratio is ``candidate.real_time_ns / baseline.real_time_ns``.  A ratio
    above one is slower.  Any source-side unavailable case, duplicate key, or
    missing counterpart makes ``result.comparable`` false; it never vanishes
    from the result simply because another case did match.
    """

    baseline_index, baseline_duplicates = _index_metrics(baseline.metrics, "baseline")
    candidate_index, candidate_duplicates = _index_metrics(candidate.metrics, "candidate")
    unavailable = list(baseline.unavailable) + list(candidate.unavailable)
    unavailable.extend(baseline_duplicates)
    unavailable.extend(candidate_duplicates)

    comparisons: list[MongoCaseComparison] = []
    for key in sorted(set(baseline_index) | set(candidate_index), key=repr):
        before = baseline_index.get(key)
        after = candidate_index.get(key)
        if before is None:
            comparisons.append(MongoCaseComparison(
                key, MetricStatus.UNMATCHED, candidate=after,
                reason="candidate metric has no baseline counterpart",
            ))
            continue
        if after is None:
            comparisons.append(MongoCaseComparison(
                key, MetricStatus.UNMATCHED, baseline=before,
                reason="baseline metric has no candidate counterpart",
            ))
            continue
        if before.real_time_ns <= 0:
            # Model validation permits zero median for a raw measurement, but
            # a ratio against it is undefined and therefore unusable.
            unavailable.append(UnavailableMetric(
                key.target, key.run_name, before.source_json,
                "baseline mean real_time must be positive for a ratio",
            ))
            comparisons.append(MongoCaseComparison(
                key, MetricStatus.UNAVAILABLE, before, after,
                reason="baseline mean real_time must be positive for a ratio",
            ))
            continue
        comparisons.append(MongoCaseComparison(
            key, MetricStatus.AVAILABLE, before, after,
            ratio=after.real_time_ns / before.real_time_ns,
        ))
    return MongoComparisonResult(tuple(comparisons), tuple(unavailable))


def _index_ferretdb_samples(
    metrics: Iterable[FerretDBBenchmarkMetric],
) -> dict[FerretDBBenchmarkKey, tuple[FerretDBBenchmarkMetric, ...]]:
    """Group repeat Go benchmark lines by their strict workload identity."""

    grouped: dict[FerretDBBenchmarkKey, list[FerretDBBenchmarkMetric]] = defaultdict(list)
    for metric in metrics:
        grouped[metric.key].append(metric)
    return {key: tuple(samples) for key, samples in grouped.items()}


def compare_ferretdb_metrics(
    baseline: FerretDBParseResult,
    candidate: FerretDBParseResult,
) -> FerretDBComparisonResult:
    """Compare native Go ``ns/op`` and ``B/op`` using mean repeat values.

    The benchmark command's ``-count`` output yields multiple native samples
    per case.  They are grouped only when their complete case name and
    ``docs-returned`` identity match.  Both sides must contain the same number
    of samples; otherwise a candidate might appear faster merely because a
    noisy repetition disappeared.  Missing allocation data is represented by
    the parser as unavailable and therefore fails closed here.
    """

    baseline_index = _index_ferretdb_samples(baseline.metrics)
    candidate_index = _index_ferretdb_samples(candidate.metrics)
    unavailable = list(baseline.unavailable) + list(candidate.unavailable)
    comparisons: list[FerretDBCaseComparison] = []

    for key in sorted(set(baseline_index) | set(candidate_index), key=repr):
        before = baseline_index.get(key, ())
        after = candidate_index.get(key, ())
        if not before:
            comparisons.append(FerretDBCaseComparison(
                key, MetricStatus.UNMATCHED, candidate_samples=after,
                reason="candidate metric has no baseline counterpart",
            ))
            continue
        if not after:
            comparisons.append(FerretDBCaseComparison(
                key, MetricStatus.UNMATCHED, baseline_samples=before,
                reason="baseline metric has no candidate counterpart",
            ))
            continue
        if len(before) != len(after):
            comparisons.append(FerretDBCaseComparison(
                key, MetricStatus.UNAVAILABLE, before, after,
                reason=(
                    "baseline and candidate have different native sample counts "
                    f"({len(before)} != {len(after)})"
                ),
            ))
            unavailable.append(UnavailableMetric(
                "FerretDB integration benchmark", key.case_name,
                before[0].source_output,
                comparisons[-1].reason,
            ))
            continue
        baseline_ns = float(fmean(sample.ns_per_op for sample in before))
        candidate_ns = float(fmean(sample.ns_per_op for sample in after))
        baseline_bytes = float(fmean(sample.bytes_per_op for sample in before))
        candidate_bytes = float(fmean(sample.bytes_per_op for sample in after))
        if baseline_ns <= 0:
            reason = "baseline mean ns/op must be positive for a ratio"
            unavailable.append(UnavailableMetric(
                "FerretDB integration benchmark", key.case_name, before[0].source_output, reason
            ))
            comparisons.append(FerretDBCaseComparison(
                key, MetricStatus.UNAVAILABLE, before, after, reason=reason
            ))
            continue
        # Zero B/op is a valid native allocation result, but no relative
        # baseline exists.  It must be surfaced, never silently ignored.
        if baseline_bytes <= 0:
            reason = "baseline mean B/op must be positive for a ratio"
            unavailable.append(UnavailableMetric(
                "FerretDB integration benchmark", key.case_name, before[0].source_output, reason
            ))
            comparisons.append(FerretDBCaseComparison(
                key, MetricStatus.UNAVAILABLE, before, after, reason=reason
            ))
            continue
        comparisons.append(FerretDBCaseComparison(
            key=key,
            status=MetricStatus.AVAILABLE,
            baseline_samples=before,
            candidate_samples=after,
            ns_per_op_ratio=candidate_ns / baseline_ns,
            bytes_per_op_ratio=candidate_bytes / baseline_bytes,
            baseline_ns_per_op_mean=baseline_ns,
            candidate_ns_per_op_mean=candidate_ns,
            baseline_bytes_per_op_mean=baseline_bytes,
            candidate_bytes_per_op_mean=candidate_bytes,
        ))
    return FerretDBComparisonResult(tuple(comparisons), tuple(unavailable))
