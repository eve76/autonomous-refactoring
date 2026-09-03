"""Per-metric distribution statistics.

The thesis evaluates quality through the *distribution* of each metric,
not through the penalty alone:

  §4.2.3 — "the quality is evaluated using the upper percentiles of the
    distribution. This measures whether the most problematic functions in
    the codebase have been brought closer to the acceptable thresholds."
  Table 4.1 — per metric: threshold, mean, median, p90, p95, p99, max;
    duplicates as a line ratio plus a block count.
  Table 5.1 — per metric: mean with percentage change from the
    unrefactored codebase, then p90/p95/p99/max.
  §4.4 — the results directory holds "the baseline and final quality
    metrics values".
  §4.5.1 — "A metric is considered cleared when all functions in the
    codebase have been brought below its threshold."

Statistics are taken over *all* functions the tools report, not only the
violating ones: Table 4.1's mean CCN of 3.21 against a threshold of 15 is
only meaningful over the whole population.
"""

from statistics import fmean
from typing import Iterable, Sequence

from analysis.tools import DuplicationResult

PER_FUNCTION_METRICS = ("ccn", "nloc", "cognitive", "param")
PERCENTILES = (90, 95, 99)

# numpy.percentile's default: linear interpolation between closest ranks.
PERCENTILE_METHOD = "linear interpolation between closest ranks"


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Percentile `q` of an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    if low == high:
        return float(sorted_values[low])
    weight = pos - low
    return float(sorted_values[low]) * (1 - weight) + float(sorted_values[high]) * weight


def _distribution(values: Iterable[float], threshold: float | None) -> dict:
    ordered = sorted(float(v) for v in values)
    stats: dict = {
        "threshold": threshold,
        "functions": len(ordered),
        "mean": round(fmean(ordered), 4) if ordered else 0.0,
        "median": round(percentile(ordered, 50), 4),
        "max": round(ordered[-1], 4) if ordered else 0.0,
    }
    for q in PERCENTILES:
        stats[f"p{q}"] = round(percentile(ordered, q), 4)
    if threshold is not None:
        over = [v for v in ordered if v > threshold]
        stats["over_threshold"] = len(over)
        stats["cleared"] = not over
    return stats


def compute_metric_stats(
    lizard_records: Iterable[dict],
    cognitive_records: Iterable[dict],
    duplication: DuplicationResult,
    thresholds: dict[str, float],
) -> dict:
    """Distribution statistics for all five metrics (Table 4.1 shape)."""
    lizard_records = list(lizard_records)
    populations: dict[str, list[float]] = {
        key: [r[key] for r in lizard_records if r.get(key) is not None]
        for key in ("ccn", "nloc", "param")
    }
    populations["cognitive"] = [
        r["cognitive"] for r in cognitive_records if r.get("cognitive") is not None
    ]

    stats = {
        key: _distribution(populations[key], thresholds.get(key))
        for key in PER_FUNCTION_METRICS
    }
    stats["duplicates"] = {
        "threshold": thresholds.get("duplicates"),
        "line_ratio": round(duplication.ratio, 6),
        "line_ratio_pct": round(duplication.ratio * 100.0, 4),
        "duplicate_lines": duplication.duplicate_lines,
        "total_lines": duplication.total_lines,
        "block_count": duplication.blocks,
        "cleared": duplication.blocks == 0,
    }
    return stats


def mean_change_pct(baseline: dict, final: dict) -> dict:
    """Percentage change in each metric's mean, as Table 5.1 reports it.

    Duplicates use the line ratio, since they have no per-function mean.
    """
    out: dict[str, float | None] = {}
    for key in PER_FUNCTION_METRICS:
        before = baseline.get(key, {}).get("mean")
        after = final.get(key, {}).get("mean")
        out[key] = round((after - before) / before * 100.0, 2) if before else None
    before = baseline.get("duplicates", {}).get("line_ratio")
    after = final.get("duplicates", {}).get("line_ratio")
    out["duplicates"] = round((after - before) / before * 100.0, 2) if before else None
    return out


def format_metric_stats(stats: dict) -> str:
    """Render the statistics as a Table 4.1-style block for the console."""
    header = (
        f"  {'metric':<11}{'thr':>5}{'mean':>9}{'median':>8}"
        f"{'p90':>8}{'p95':>8}{'p99':>8}{'max':>8}{'over':>6}"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]
    for key in PER_FUNCTION_METRICS:
        s = stats.get(key)
        if not s:
            continue
        lines.append(
            f"  {key:<11}{s['threshold']:>5}{s['mean']:>9.2f}{s['median']:>8.1f}"
            f"{s['p90']:>8.1f}{s['p95']:>8.1f}{s['p99']:>8.1f}{s['max']:>8.1f}"
            f"{s.get('over_threshold', 0):>6}"
        )
    d = stats.get("duplicates")
    if d:
        lines.append(
            f"  {'duplicates':<11}{d['threshold']:>5}"
            f"{d['line_ratio_pct']:>8.2f}%  blocks={d['block_count']}"
            f"  ({d['duplicate_lines']}/{d['total_lines']} lines)"
        )
    return "\n".join(lines)
