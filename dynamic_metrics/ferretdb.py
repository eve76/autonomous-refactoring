"""Offline support for FerretDB's existing native Go integration benchmarks.

This module does *not* start PostgreSQL, connect to a service, spawn ``go``,
or collect process-level measurements.  It only (1) creates a redacted command
template for the existing ``BenchmarkFind`` and ``BenchmarkInsert`` tests and
(2) parses their native Go output.  The only performance values retained are
``ns/op`` and the Go ``-benchmem`` value ``B/op``.

Actual execution is intentionally left to a later, explicitly authorised
runner.  That runner must provide a dedicated PostgreSQL URL through the
configured environment-variable name, keep it out of command logs/results,
and use a stable external database.  This design follows the flags declared
in ``FerretDB/integration/setup/setup.go`` and the development build tag in
``FerretDB/integration/Taskfile.yml``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Mapping

from .models import (
    DynamicMetricError,
    FerretDBBenchmarkKey,
    FerretDBBenchmarkMetric,
    FerretDBParseResult,
    UnavailableMetric,
)


DEFAULT_POSTGRESQL_URL_ENV_VAR = "FERRETDB_BENCHMARK_POSTGRESQL_URL"
DEFAULT_BENCHMARKS = ("BenchmarkFind", "BenchmarkInsert")
_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BENCHMARK_LINE_RE = re.compile(
    r"^\s*(?P<name>Benchmark\S+)\s+(?P<iterations>[1-9][0-9]*)\s+(?P<metrics>.+?)\s*$"
)
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_NS_PER_OP_RE = re.compile(rf"(?<!\S)(?P<value>{_NUMBER})\s+ns/op(?:\s|$)")
_BYTES_PER_OP_RE = re.compile(rf"(?<!\S)(?P<value>{_NUMBER})\s+B/op(?:\s|$)")
_DOCS_RETURNED_RE = re.compile(rf"(?<!\S)(?P<value>{_NUMBER})\s+docs-returned(?:\s|$)")


@dataclass(frozen=True)
class FerretDBBenchmarkConfig:
    """Non-secret configuration for a future, externally authorised run.

    The PostgreSQL URL itself is never a field of this object.  Only the name
    of the environment variable that supplies it at execution time is kept.
    ``enabled`` defaults to false so copying this experimental profile cannot
    accidentally connect to an external database.
    """

    enabled: bool = False
    postgresql_url_env_var: str = DEFAULT_POSTGRESQL_URL_ENV_VAR
    repetitions: int = 7
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS

    def __post_init__(self) -> None:
        if not _ENV_VAR_RE.fullmatch(self.postgresql_url_env_var):
            raise DynamicMetricError("postgresql_url_env_var must be a valid environment variable name")
        if self.repetitions <= 0:
            raise DynamicMetricError("repetitions must be positive")
        if not self.benchmarks:
            raise DynamicMetricError("at least one FerretDB benchmark must be selected")
        invalid = set(self.benchmarks).difference(DEFAULT_BENCHMARKS)
        if invalid:
            raise DynamicMetricError(
                "FerretDB dynamic metrics may use only existing integration benchmarks: "
                + ", ".join(sorted(invalid))
            )


@dataclass(frozen=True)
class FerretDBBenchmarkAvailability:
    """Whether a command may be prepared without disclosing any URL value."""

    available: bool
    reason: str
    postgresql_url_env_var: str


@dataclass(frozen=True)
class FerretDBBenchmarkCommand:
    """A redacted, shell-style command template for a future runner.

    ``argv_template`` contains the literal ``${NAME}`` placeholder, never its
    value.  It is safe to put this template in configuration or result
    manifests.  A future execution layer must expand that *single* placeholder
    in-memory and redact it from logs; it must not serialise a resolved argv.
    """

    working_directory: str
    argv_template: tuple[str, ...]
    postgresql_url_env_var: str


def ferretdb_benchmark_availability(
    config: FerretDBBenchmarkConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> FerretDBBenchmarkAvailability:
    """Validate opt-in configuration without returning the secret URL value.

    An absent or blank URL environment variable is explicitly unavailable.
    This function reads the environment only to determine availability; it
    never exposes, embeds, logs, or stores its value.
    """

    if not config.enabled:
        return FerretDBBenchmarkAvailability(
            False, "FerretDB dynamic benchmarks are disabled pending external-service authorisation",
            config.postgresql_url_env_var,
        )
    values = os.environ if environ is None else environ
    if not values.get(config.postgresql_url_env_var, "").strip():
        return FerretDBBenchmarkAvailability(
            False,
            "PostgreSQL URL environment variable is absent or blank",
            config.postgresql_url_env_var,
        )
    return FerretDBBenchmarkAvailability(True, "available", config.postgresql_url_env_var)


def build_ferretdb_benchmark_command(
    ferretdb_root: str | PathLike[str],
    config: FerretDBBenchmarkConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> FerretDBBenchmarkCommand:
    """Build a redacted command for the two existing Go integration benchmarks.

    No command is executed here.  A configuration must be explicitly enabled
    and its URL env-var present; otherwise callers receive ``DynamicMetricError``
    rather than a command that may later be run against an unknown service.
    The returned URL argument is a literal environment-variable placeholder,
    so no secret can enter config, artifacts, or normal command logging.
    """

    availability = ferretdb_benchmark_availability(config, environ=environ)
    if not availability.available:
        raise DynamicMetricError(availability.reason)
    root = Path(ferretdb_root)
    integration = root / "integration"
    if not integration.is_dir():
        raise DynamicMetricError(f"FerretDB integration directory does not exist: {integration}")
    selected = "|".join(re.escape(name) for name in config.benchmarks)
    return FerretDBBenchmarkCommand(
        # `integration/` is a separate Go module in this checkout.  Running
        # `go test ./integration` from the repository root therefore fails
        # with "main module ... does not contain package .../integration".
        working_directory=str(integration),
        argv_template=(
            # The two approved benchmarks live in the integration module's
            # root package; do not compile unrelated integration subpackages.
            "go", "test", "-buildvcs=false", "-tags=ferretdb_dev", ".", "-run=^$",
            f"-bench=^({selected})$", "-benchmem", f"-count={config.repetitions}",
            "-target-backend=ferretdb",
            f"-postgresql-url=${{{config.postgresql_url_env_var}}}",
            # Benchmark output is the measurement protocol. Disable the
            # integration harness's OTLP exporter so asynchronous exporter
            # errors cannot splice text into Go benchmark result lines.
            "-otel-traces-url=",
        ),
        postgresql_url_env_var=config.postgresql_url_env_var,
    )


def _metric_value(pattern: re.Pattern[str], text: str, metric_name: str, case_name: str) -> float:
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise DynamicMetricError(f"{case_name}: expected exactly one {metric_name} value")
    value = float(matches[0])
    if value < 0:
        raise DynamicMetricError(f"{case_name}: {metric_name} must not be negative")
    return value


def _case_name_without_cpu_suffix(raw_name: str) -> str:
    """Remove Go's terminal ``-N`` CPU-count suffix, not case path segments."""

    return re.sub(r"-[1-9][0-9]*$", "", raw_name)


def parse_ferretdb_benchmark_text(
    text: str,
    *,
    source_output: str = "<memory>",
    run_id: str = "",
) -> FerretDBParseResult:
    """Parse native ``go test -bench -benchmem`` output without extra probes.

    Every selected benchmark output line yields one raw sample.  Repeated
    ``-count`` lines remain separate; :func:`compare_ferretdb_metrics` later
    derives medians from matching sample sets.  ``allocs/op`` may appear in
    Go output but is intentionally ignored.  ``docs-returned`` is preserved
    solely in the comparison key.
    """

    metrics: list[FerretDBBenchmarkMetric] = []
    unavailable: list[UnavailableMetric] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _BENCHMARK_LINE_RE.match(line)
        if match is None:
            continue
        raw_name = match.group("name")
        case_name = _case_name_without_cpu_suffix(raw_name)
        if not any(case_name == name or case_name.startswith(f"{name}/") for name in DEFAULT_BENCHMARKS):
            unavailable.append(UnavailableMetric(
                "FerretDB integration benchmark", raw_name, source_output,
                f"line {line_number}: unexpected benchmark case",
            ))
            continue
        try:
            ns_per_op = _metric_value(_NS_PER_OP_RE, match.group("metrics"), "ns/op", case_name)
            if ns_per_op <= 0:
                raise DynamicMetricError(f"{case_name}: ns/op must be positive")
            bytes_per_op = _metric_value(_BYTES_PER_OP_RE, match.group("metrics"), "B/op", case_name)
            docs_matches = _DOCS_RETURNED_RE.findall(match.group("metrics"))
            if len(docs_matches) > 1:
                raise DynamicMetricError(f"{case_name}: expected at most one docs-returned value")
            docs_returned = float(docs_matches[0]) if docs_matches else None
            if docs_returned is not None and docs_returned < 0:
                raise DynamicMetricError(f"{case_name}: docs-returned must not be negative")
            metrics.append(FerretDBBenchmarkMetric(
                key=FerretDBBenchmarkKey(case_name, docs_returned),
                ns_per_op=ns_per_op,
                bytes_per_op=bytes_per_op,
                iterations=int(match.group("iterations")),
                source_output=source_output,
                run_id=run_id,
            ))
        except DynamicMetricError as exc:
            unavailable.append(UnavailableMetric(
                "FerretDB integration benchmark", case_name, source_output,
                f"line {line_number}: {exc}",
            ))
    if not metrics and not unavailable:
        unavailable.append(UnavailableMetric(
            "FerretDB integration benchmark", "all-cases", source_output,
            "no Go benchmark result lines found",
        ))
    return FerretDBParseResult(tuple(metrics), tuple(unavailable))
