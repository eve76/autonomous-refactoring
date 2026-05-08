"""Wrappers around the static-analysis tools used by the multi-agent
system.

Per-function metrics are merged by (file, function name):
  - Lizard       -> CCN, NLOC, parameter count
  - mod-cog-cmpl -> cognitive complexity (C/C++)

Codebase-level metric:
  - Duplo        -> duplicate-line ratio (computed from JSON output)

run_static_analysis returns (per_function_records, duplicate_ratio).
If a tool fails to run (binary missing, parse error, ...) the failure
is logged and that metric is silently dropped from the records, so
the overall pipeline degrades gracefully.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import lizard

try:
    from modified_cognitive_complexity import cognitive_complexity_for_file
    _HAS_COGNITIVE = True
except ImportError:
    _HAS_COGNITIVE = False


_DEFAULT_EXCLUDE = ("test", "tests", "__pycache__", "build", "node_modules", ".git")
_CPP_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx")


def _is_excluded(path: str) -> bool:
    parts = Path(path).parts
    return any(p in _DEFAULT_EXCLUDE for p in parts)


def _iter_cpp_files(target: Path):
    if target.is_file():
        yield target
        return
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _CPP_SUFFIXES:
            continue
        if _is_excluded(str(path.relative_to(target))):
            continue
        yield path


def run_lizard(target: Path) -> list[dict]:
    records: list[dict] = []
    for file_info in lizard.analyze([str(target)]):
        if _is_excluded(file_info.filename):
            continue
        for func in file_info.function_list:
            records.append({
                "file": file_info.filename,
                "line": func.start_line,
                "name": func.name,
                "ccn": func.cyclomatic_complexity,
                "nloc": func.nloc,
                "param": func.parameter_count,
            })
    return records


def run_cognitive(target: Path) -> dict[tuple[str, str], int]:
    """Map (file, function name) -> cognitive complexity."""
    if not _HAS_COGNITIVE:
        return {}
    out: dict[tuple[str, str], int] = {}
    for path in _iter_cpp_files(target):
        try:
            scores = cognitive_complexity_for_file(path)
        except Exception as exc:
            print(f"[tools] cognitive failed on {path}: {exc}", file=sys.stderr)
            continue
        for raw_name, score in scores.items():
            if raw_name is None:
                continue
            name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, (bytes, bytearray)) else str(raw_name)
            out[(str(path), name)] = int(score)
    return out


def run_duplo(target: Path, duplo_binary: str, min_block_lines: int = 6) -> float:
    """Return duplicate-line ratio (0..1) for the codebase, via Duplo."""
    if not duplo_binary or not shutil.which(duplo_binary):
        return 0.0
    files = list(_iter_cpp_files(target))
    if not files:
        return 0.0
    file_list = "\n".join(str(p) for p in files)
    try:
        proc = subprocess.run(
            [duplo_binary, "-ml", str(min_block_lines), "-ip", "-json", "-", "-"],
            input=file_list,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[tools] duplo failed: {exc}", file=sys.stderr)
        return 0.0
    try:
        blocks = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        return 0.0
    if not isinstance(blocks, list):
        blocks = []
    duplicate_lines = sum(int(b.get("LineCount", 0)) * 2 for b in blocks)
    total_lines = 0
    for path in files:
        try:
            total_lines += sum(1 for _ in path.open(errors="replace"))
        except OSError:
            continue
    if total_lines == 0:
        return 0.0
    return duplicate_lines / total_lines


def _function_match_keys(name: str) -> set[str]:
    """Generate possible match keys for a function name.

    Lizard reports class-qualified names (e.g. `Factory::compileInfo`),
    while the cognitive-complexity tool reports the bare function name
    (`compileInfo`). Returning both lets us merge regardless of which
    style each tool produced.
    """
    keys = {name}
    if "::" in name:
        keys.add(name.rsplit("::", 1)[-1])
    return keys


def run_static_analysis(
    target: Path,
    thresholds: dict,
    duplo_binary: str = "",
    duplo_min_block_lines: int = 6,
) -> tuple[list[dict], float]:
    records = run_lizard(target)
    cog = run_cognitive(target)

    if cog:
        cog_by_name: dict[str, list[int]] = {}
        for (path, name), score in cog.items():
            cog_by_name.setdefault((path, name), []).append(score)
        for rec in records:
            for nm in _function_match_keys(rec["name"]):
                if (rec["file"], nm) in cog:
                    rec["cognitive"] = cog[(rec["file"], nm)]
                    break

    dup_ratio = run_duplo(target, duplo_binary, duplo_min_block_lines)
    return records, dup_ratio
