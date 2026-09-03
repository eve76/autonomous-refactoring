"""Immutable data contracts for native benchmark comparisons.

This module contains no subprocess or repository orchestration; the runner
and merge gate consume these serialisable comparison objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class DynamicMetricError(ValueError):
    """Base class for data which must not be treated as a valid measurement."""


class MetricStatus(str, Enum):
    """A fail-closed status for one logical benchmark case."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class BuildIdentity:
    """Build inputs which have to match before a performance comparison.

    ``build_type`` normally comes from Google Benchmark's
    ``context.library_build_type``.  ``build_flags`` is supplied by the
    runner because benchmark JSON does not contain Bazel options.
    """

    build_type: str
    build_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.build_type:
            raise DynamicMetricError("build_type is required for a comparable measurement")


@dataclass(frozen=True)
class EnvironmentIdentity:
    """Stable machine characteristics from Google Benchmark JSON context.

    Date and load average are deliberately retained as provenance elsewhere,
    not used as identity: they naturally differ between baseline and candidate
    runs.  Host, CPU count, CPU MHz and CPU-scaling mode must remain equal.
    """

    host_name: str
    num_cpus: int
    mhz_per_cpu: float | None
    cpu_scaling_enabled: bool | None

    def __post_init__(self) -> None:
        if not self.host_name:
            raise DynamicMetricError("host_name is required for a comparable measurement")
        if self.num_cpus <= 0:
            raise DynamicMetricError("num_cpus must be a positive integer")


@dataclass(frozen=True)
class MongoBenchmarkKey:
    """Strict identity of one MongoDB aggregate benchmark measurement.

    The workload label is included in addition to the required target,
    run-name, threads, aggregate, build, and environment identities.  This
    prevents cases that re-use a run name while varying a label from being
    accidentally averaged together.
    """

    target: str
    run_name: str
    threads: int
    aggregate_name: str
    workload_label: str
    build: BuildIdentity
    environment: EnvironmentIdentity

    def __post_init__(self) -> None:
        # Import lazily: mongodb imports these immutable contracts, while this
        # contract must share its *canonical* scope predicate with discovery
        # and the runtime runner rather than duplicate a prefix check.
        from .mongodb import is_query_benchmark_target
        if not is_query_benchmark_target(self.target):
            raise DynamicMetricError(f"target is outside MongoDB query scope: {self.target}")
        if not self.run_name:
            raise DynamicMetricError("run_name is required")
        if self.threads <= 0:
            raise DynamicMetricError("threads must be a positive integer")
        if self.aggregate_name != "mean":
            raise DynamicMetricError("MongoDB dynamic metric must use aggregate_name='mean'")


@dataclass(frozen=True)
class MongoRealTimeMetric:
    """The sole MongoDB dynamic metric: aggregate-mean real time in ns/op."""

    key: MongoBenchmarkKey
    real_time_ns: float
    mean_real_time_ns: float
    stddev_real_time_ns: float
    coefficient_of_variation: float
    iterations: int
    source_json: str
    date: str = ""
    executable: str = ""
    commit: str = ""
    run_id: str = ""

    def __post_init__(self) -> None:
        for name in ("real_time_ns", "mean_real_time_ns", "stddev_real_time_ns"):
            value = getattr(self, name)
            if value < 0:
                raise DynamicMetricError(f"{name} must not be negative")
        if self.mean_real_time_ns <= 0:
            raise DynamicMetricError("mean_real_time_ns must be positive for CV")
        if self.coefficient_of_variation < 0:
            raise DynamicMetricError("coefficient_of_variation must not be negative")
        if self.iterations <= 0:
            raise DynamicMetricError("iterations must be positive")


@dataclass(frozen=True)
class UnavailableMetric:
    """A logical case whose data cannot safely participate in comparison."""

    target: str
    case_hint: str
    source_json: str
    reason: str


@dataclass(frozen=True)
class MongoParseResult:
    """All usable metrics and all parse failures from a JSON artifact set.

    Consumers must check :pyattr:`complete` before treating the result as
    comparable.  The boolean is intentionally fail-closed.
    """

    metrics: tuple[MongoRealTimeMetric, ...]
    unavailable: tuple[UnavailableMetric, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unavailable and bool(self.metrics)


@dataclass(frozen=True)
class MongoCaseComparison:
    """A baseline/candidate pairing, or an explicit reason it is unusable."""

    key: MongoBenchmarkKey
    status: MetricStatus
    baseline: MongoRealTimeMetric | None = None
    candidate: MongoRealTimeMetric | None = None
    ratio: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class MongoComparisonResult:
    """Comparison summary that cannot silently discard bad or missing data."""

    comparisons: tuple[MongoCaseComparison, ...]
    unavailable: tuple[UnavailableMetric, ...] = ()

    @property
    def comparable(self) -> bool:
        return (
            bool(self.comparisons)
            and not self.unavailable
            and all(case.status is MetricStatus.AVAILABLE for case in self.comparisons)
        )

    @property
    def matched(self) -> tuple[MongoCaseComparison, ...]:
        return tuple(case for case in self.comparisons if case.status is MetricStatus.AVAILABLE)

    def to_dict(self) -> dict[str, Any]:
        """Return a stdlib-JSON-ready representation for a future artifact writer."""

        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return convert(self)


@dataclass(frozen=True)
class FerretDBBenchmarkKey:
    """Identity of one native Go benchmark workload.

    ``case_name`` is the complete Go benchmark name after removing only the
    Go CPU suffix (for example ``BenchmarkFind/ferretdb/Int32IDIndex``).
    ``docs_returned`` is retained only to prevent a Find workload whose result
    cardinality changed from being compared to a different workload.  It is
    not a performance metric and never contributes to a penalty.
    """

    case_name: str
    docs_returned: float | None = None

    def __post_init__(self) -> None:
        if not self.case_name.startswith(("BenchmarkFind", "BenchmarkInsert")):
            raise DynamicMetricError(
                "FerretDB benchmark case must start with BenchmarkFind or BenchmarkInsert"
            )
        if self.docs_returned is not None and self.docs_returned < 0:
            raise DynamicMetricError("docs_returned must not be negative")


@dataclass(frozen=True)
class FerretDBBenchmarkMetric:
    """One native Go benchmark sample, using only time and allocation bytes.

    The Go tool emits ``ns/op`` and, with ``-benchmem``, ``B/op`` directly.
    ``iterations`` is provenance from the benchmark line.  ``allocs/op`` is
    deliberately not stored: the agreed dynamic penalty consumes only
    ``ns/op`` and ``B/op`` for FerretDB.
    """

    key: FerretDBBenchmarkKey
    ns_per_op: float
    bytes_per_op: float
    iterations: int
    source_output: str
    run_id: str = ""

    def __post_init__(self) -> None:
        if self.ns_per_op <= 0:
            raise DynamicMetricError("ns_per_op must be positive")
        if self.bytes_per_op < 0:
            raise DynamicMetricError("bytes_per_op must not be negative")
        if self.iterations <= 0:
            raise DynamicMetricError("iterations must be positive")


@dataclass(frozen=True)
class FerretDBParseResult:
    """Parsed native Go benchmark samples and any fail-closed parse failures."""

    metrics: tuple[FerretDBBenchmarkMetric, ...]
    unavailable: tuple[UnavailableMetric, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unavailable and bool(self.metrics)


@dataclass(frozen=True)
class FerretDBCaseComparison:
    """Mean comparison for one exact FerretDB workload identity.

    ``baseline_samples`` and ``candidate_samples`` preserve every native Go
    sample, while the two means are the values a later penalty stage can
    compare.  A ratio above one means a candidate regression.
    """

    key: FerretDBBenchmarkKey
    status: MetricStatus
    baseline_samples: tuple[FerretDBBenchmarkMetric, ...] = ()
    candidate_samples: tuple[FerretDBBenchmarkMetric, ...] = ()
    ns_per_op_ratio: float | None = None
    bytes_per_op_ratio: float | None = None
    baseline_ns_per_op_mean: float | None = None
    candidate_ns_per_op_mean: float | None = None
    baseline_bytes_per_op_mean: float | None = None
    candidate_bytes_per_op_mean: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class FerretDBComparisonResult:
    """Fail-closed comparison summary for FerretDB Go benchmark data."""

    comparisons: tuple[FerretDBCaseComparison, ...]
    unavailable: tuple[UnavailableMetric, ...] = ()

    @property
    def comparable(self) -> bool:
        return (
            bool(self.comparisons)
            and not self.unavailable
            and all(case.status is MetricStatus.AVAILABLE for case in self.comparisons)
        )

    @property
    def matched(self) -> tuple[FerretDBCaseComparison, ...]:
        return tuple(case for case in self.comparisons if case.status is MetricStatus.AVAILABLE)

    def to_dict(self) -> dict[str, Any]:
        """Return a stdlib-JSON-ready representation without command secrets."""

        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if hasattr(value, "__dataclass_fields__"):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return convert(self)
