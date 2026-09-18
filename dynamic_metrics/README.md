# Dynamic benchmark metric boundary

This package runs only the repositories' existing native benchmarks, parses
their output, and writes redacted rolling-baseline/candidate comparison artifacts. It does
not start database services, invoke profilers, or collect process-level
measurements.

MongoDB Query consumes only Google Benchmark aggregate-mean `real_time` from
seven repetitions. Its coefficient of variation is retained as diagnostic
metadata; high CV is reported but never makes a measurement unavailable or
rejects a merge.
FerretDB consumes the seven-repetition arithmetic means of the values its
existing Go integration benchmarks emit: `ns/op` and, with Go's native
`-benchmem` flag, `B/op`. `docs-returned` is
workload identity for `BenchmarkFind`, not a penalty metric. `allocs/op` is
intentionally not part of the FerretDB metric model or comparison.

FerretDB execution remains disabled by default. When dynamic mode is enabled,
the runner requires a stable external PostgreSQL service URL through the
configured environment-variable name (default:
`FERRETDB_BENCHMARK_POSTGRESQL_URL`). The URL value is expanded only at the
subprocess boundary and is redacted from configuration, command artifacts,
logs, summaries, and result JSON.

The expected benchmark template selects only FerretDB's existing
`BenchmarkFind` and `BenchmarkInsert` integration benchmarks, and includes
`-run=^$`, `-benchmem`, `-count`, `-tags=ferretdb_dev`, and
`-target-backend=ferretdb`. Correctness tests remain a separate phase and
retain their existing race-enabled configuration.

MongoDB dynamically discovers the available query benchmarks, then selects
the verified existing `//src/mongo/db/query:canonical_query_bm` target. Its
eight parameterised cases provide aggregate-mean `real_time` through Google
Benchmark JSON. The initial integration commit is measured once; every gate
measures only the candidate and promotes that measurement to the next baseline
only after a successful fast-forward merge.
