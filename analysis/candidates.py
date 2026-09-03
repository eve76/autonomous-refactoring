"""Conservative local static-analysis leads for Analyst sessions.

These leads are hints, not backlog issues. Analysts remain responsible for
inspecting the code and emitting the existing ISSUE format only when they
independently confirm that a refactoring is appropriate.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from analysis.penalty import function_penalty


LIZARD_METRICS = ("ccn", "nloc", "param")
LOCAL_LEAD_METRICS = (*LIZARD_METRICS, "cognitive")


@dataclass(frozen=True)
class LocalLead:
    key: str
    file_path: str
    line: int
    function: str
    metric_values: dict[str, float]
    estimated_reduction: float

    def to_dict(self) -> dict:
        return asdict(self)


def _relative_file(raw: str, target: Path) -> str:
    path = Path(raw)
    try:
        return str(path.resolve().relative_to(target.resolve()))
    except (OSError, ValueError):
        return str(path)


def _lead_key(
    file_path: str,
    line: int,
    function: str,
    metric_values: dict[str, float],
) -> str:
    metrics = ",".join(
        f"{name}={metric_values[name]:g}" for name in sorted(metric_values)
    )
    raw = f"{file_path}|{line}|{function}|{metrics}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_local_leads(
    lizard_records: list[dict],
    cognitive_records: list[dict] | None = None,
    *,
    target: Path,
    thresholds: dict,
    weights: dict,
) -> list[LocalLead]:
    """Return deterministic, non-authoritative per-function metric leads."""
    candidates: dict[tuple[str, str], dict] = {}
    for source, metrics in (
        (lizard_records, LIZARD_METRICS),
        (cognitive_records or [], ("cognitive",)),
    ):
        for record in source:
            file_path = _relative_file(str(record.get("file", "")), target)
            function = str(record.get("name", "(unknown)"))
            key = (file_path, function)
            candidate = candidates.setdefault(key, {
                "file_path": file_path,
                "line": 0,
                "function": function,
                "metric_values": {},
                "estimated_reduction": 0.0,
            })
            line = int(record.get("line", 0) or 0)
            if line and not candidate["line"]:
                candidate["line"] = line
            for metric in metrics:
                value = float(record.get(metric, 0) or 0)
                threshold = float(thresholds.get(metric, 0))
                weight = float(weights.get(metric, 1))
                if weight <= 0 or value <= threshold:
                    continue
                candidate["metric_values"][metric] = value
                candidate["estimated_reduction"] += (
                    weight * function_penalty(value, threshold)
                )

    leads = []
    for candidate in candidates.values():
        values = candidate["metric_values"]
        if not values:
            continue
        file_path = candidate["file_path"]
        line = candidate["line"]
        function = candidate["function"]
        leads.append(LocalLead(
            key=_lead_key(file_path, line, function, values),
            file_path=file_path,
            line=line,
            function=function,
            metric_values=values,
            estimated_reduction=round(candidate["estimated_reduction"], 4),
        ))

    return sorted(
        leads,
        key=lambda item: (
            -item.estimated_reduction,
            item.file_path,
            item.line,
            item.function,
        ),
    )

def lead_matches_focus(lead: LocalLead, focus: list[str]) -> bool:
    """Match either metric names or directory/file hints from Orchestrator."""
    normalized = [item.strip().lower() for item in focus if item.strip()]
    if not normalized:
        return True

    metric_focus = {item for item in normalized if item in LOCAL_LEAD_METRICS}
    path_or_function_focus = [
        item for item in normalized if item not in LOCAL_LEAD_METRICS
    ]
    if metric_focus and not metric_focus.intersection(lead.metric_values):
        return False
    if path_or_function_focus and not any(
        item in lead.file_path.lower() or item in lead.function.lower()
        for item in path_or_function_focus
    ):
        return False
    return True


def format_local_leads(leads: list[LocalLead]) -> str:
    if not leads:
        return "  (no local static-analysis leads for this assignment)"
    rows = []
    for index, lead in enumerate(leads, 1):
        metrics = ", ".join(
            f"{name.upper()}={value:g}"
            for name, value in sorted(lead.metric_values.items())
        )
        rows.append(
            f"  {index}. {lead.file_path}:{lead.line} "
            f"{lead.function} — {metrics}; "
            f"estimated reduction={lead.estimated_reduction:.2f}"
        )
    return "\n".join(rows)
