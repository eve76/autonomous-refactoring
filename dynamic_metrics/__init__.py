"""Dynamic benchmark collection, parsing, and comparison support.

MongoDB contributes aggregate-mean ``real_time``; FerretDB contributes Go
``ns/op`` and ``B/op`` from its native integration benchmarks.
"""

from .compare import compare_ferretdb_metrics, compare_mongodb_real_time
from .models import (
    BuildIdentity,
    DynamicMetricError,
    EnvironmentIdentity,
    FerretDBBenchmarkKey,
    FerretDBBenchmarkMetric,
    FerretDBCaseComparison,
    FerretDBComparisonResult,
    FerretDBParseResult,
    MetricStatus,
    MongoBenchmarkKey,
    MongoCaseComparison,
    MongoComparisonResult,
    MongoParseResult,
    MongoRealTimeMetric,
    UnavailableMetric,
)
from .ferretdb import (
    DEFAULT_BENCHMARKS,
    DEFAULT_POSTGRESQL_URL_ENV_VAR,
    FerretDBBenchmarkAvailability,
    FerretDBBenchmarkCommand,
    FerretDBBenchmarkConfig,
    build_ferretdb_benchmark_command,
    ferretdb_benchmark_availability,
    parse_ferretdb_benchmark_text,
)
from .mongodb import (
    APPROVED_DYNAMIC_QUERY_BENCHMARKS,
    QUERY_PACKAGE,
    approved_query_dynamic_benchmark_targets,
    discover_query_benchmark_targets,
    is_query_benchmark_target,
    parse_mongodb_benchmark_artifacts,
    parse_mongodb_benchmark_json,
)
from .runner import (
    DynamicBaselineResult, DynamicBenchmarkRunner, DynamicEvaluation,
    ExecutionResult,
)

__all__ = [
    "BuildIdentity", "DynamicMetricError", "EnvironmentIdentity", "MetricStatus",
    "DEFAULT_BENCHMARKS", "DEFAULT_POSTGRESQL_URL_ENV_VAR",
    "FerretDBBenchmarkAvailability", "FerretDBBenchmarkCommand", "FerretDBBenchmarkConfig",
    "FerretDBBenchmarkKey", "FerretDBBenchmarkMetric", "FerretDBCaseComparison",
    "FerretDBComparisonResult", "FerretDBParseResult",
    "MongoBenchmarkKey", "MongoCaseComparison", "MongoComparisonResult",
    "MongoParseResult", "MongoRealTimeMetric", "QUERY_PACKAGE", "UnavailableMetric",
    "APPROVED_DYNAMIC_QUERY_BENCHMARKS",
    "build_ferretdb_benchmark_command", "compare_ferretdb_metrics",
    "compare_mongodb_real_time", "discover_query_benchmark_targets",
    "approved_query_dynamic_benchmark_targets",
    "ferretdb_benchmark_availability",
    "is_query_benchmark_target", "parse_mongodb_benchmark_artifacts",
    "parse_mongodb_benchmark_json", "parse_ferretdb_benchmark_text",
    "DynamicBaselineResult", "DynamicBenchmarkRunner", "DynamicEvaluation",
    "ExecutionResult",
]
