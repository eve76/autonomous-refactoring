"""Hyperbolic penalty function (thesis Equations 4.4 and 4.5).

Per-function metrics (CCN, Cog, LLOC, Param):
    p_m(x) = 100 * (1 - T_m / max(T_m, x))
Codebase-level duplicate-line ratio:
    p_dup(r) = 100 * r / (r + k),  k = 0.1

Total penalty = sum of all per-function penalties (over functions and
metrics) + the duplicate-ratio penalty.

The same function is used by both the merge gate and the backlog
impact estimator, so they remain consistent.
"""

import re
from typing import Iterable

DUP_K = 0.1

# Per-function metrics that use Eq 4.4. The dict key in `thresholds`
# matches the field name in each metric record produced by tools.py.
PER_FUNCTION_METRICS = ("ccn", "cognitive", "nloc", "param")


def function_penalty(value: float, threshold: float) -> float:
    """Hyperbolic penalty (paper Eq 4.4): zero at/below threshold, grows toward 100."""
    if value <= threshold:
        return 0.0
    return 100.0 * (1.0 - threshold / max(threshold, value))


def duplicate_penalty(ratio: float) -> float:
    """Hyperbolic saturation (paper Eq 4.5): no threshold, k = 0.1."""
    if ratio <= 0.0:
        return 0.0
    return 100.0 * ratio / (ratio + DUP_K)


def compute_total_penalty(
    metrics: Iterable[dict],
    thresholds: dict[str, float],
    duplicate_ratio: float = 0.0,
) -> float:
    total = 0.0
    for m in metrics:
        for key in PER_FUNCTION_METRICS:
            if key not in thresholds:
                continue
            value = m.get(key)
            if value is None:
                continue
            total += function_penalty(float(value), float(thresholds[key]))
    total += duplicate_penalty(float(duplicate_ratio))
    return total


_METRIC_NAME = r"CCN|cognitive|NLOC|LLOC|param(?:eter)?s?|duplicate[s]?"
# Matches both `name=value` (e.g. "CCN=27") and `value name` (e.g. "161 NLOC")
# as the analyst prompt example mixes the two styles.
_METRIC_RE = re.compile(
    rf"(?:({_METRIC_NAME})\s*=\s*([\d.]+))"
    rf"|(?:([\d.]+)\s+({_METRIC_NAME})\b)",
    re.IGNORECASE,
)

_KEY_ALIAS = {
    "ccn": "ccn",
    "cognitive": "cognitive",
    "nloc": "nloc",
    "lloc": "nloc",
    "param": "param",
    "params": "param",
    "parameter": "param",
    "parameters": "param",
    "duplicate": "duplicates",
    "duplicates": "duplicates",
}


def estimate_reduction_from_message(
    message: str, thresholds: dict[str, float]
) -> tuple[float, dict[str, float]]:
    """Parse metric values out of an analyst-issued message.

    The estimated reduction assumes the programmer brings each metric
    down to its threshold (so the per-function penalty drops to 0).
    For duplicates, assumes the duplicate block is removed (penalty
    contribution -> 0).
    """
    parsed: dict[str, float] = {}
    for n1, v1, v2, n2 in _METRIC_RE.findall(message):
        name, value = (n1, v1) if n1 else (n2, v2)
        key = _KEY_ALIAS.get(name.lower())
        if key is None:
            continue
        parsed[key] = float(value)

    reduction = 0.0
    for key, value in parsed.items():
        threshold = thresholds.get(key)
        if threshold is None:
            continue
        if key in PER_FUNCTION_METRICS:
            reduction += function_penalty(value, threshold)
        elif key == "duplicates":
            # Assume eliminating the block removes its share of the
            # current duplicate-ratio penalty. The message normally
            # carries the percentage (e.g. duplicates=2.4); convert.
            ratio = value / 100.0 if value > 1.0 else value
            reduction += duplicate_penalty(ratio)
    return reduction, parsed
