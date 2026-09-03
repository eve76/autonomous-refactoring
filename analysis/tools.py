"""Wrappers around the static-analysis tools used by the multi-agent
system (thesis §4.5.1).

Lizard is invoked as a CLI subprocess against a file list (`-l cpp -f`).
Cognitive complexity uses modified_cognitive_complexity for C/C++ and
the gocognit CLI for Go.
Duplication uses language-specific CLI subprocesses: dupl for Go and
Duplo for C/C++. Per-function records are returned as two
separate lists (lizard and cognitive) — they are combined later by
the penalty function, not joined here.

run_static_analysis returns (lizard_records, cognitive_records, dup_ratio).
"""

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from modified_cognitive_complexity import cognitive_complexity_for_file
    _HAS_COGNITIVE = True
except ImportError:
    _HAS_COGNITIVE = False


class StaticAnalysisError(RuntimeError):
    """A configured metric tool failed, so the gate must fail closed."""


# Thesis §4.3.2: "Test directories are excluded since they do not
# contribute to the penalty score." The thesis says *directories*, plural,
# and never enumerates them, so this list is a parameter of the
# experiment rather than something the thesis pins down — override it with
# `--exclude-dirs` to match the target repository's own layout.
#
# Matching only a directory named exactly `test` was too narrow to be a
# safe default: a repository using `tests/` would have its test files
# measured, so the analyst would file issues against them while the
# programmer prompt forbids touching test files — issues that can only
# ever be skipped. Every name below is a directory that is a test
# artifact by convention, never production code.
#
# Changing this changes measured penalty. Table 4.1 is the check: the
# target codebase must still report 7 duplicate blocks at a 2.41%
# duplicate-line ratio.
DEFAULT_EXCLUDE_DIRS = (
    "test", "tests", "testing",
    "unittest", "unittests", "unit_test", "unit_tests",
    "gtest", "googletest", "gmock", "mocks",
)


def _exclude_set(exclude_dirs=None) -> frozenset:
    """Normalize an exclusion list to a lowercase set for path matching.

    None means "use the default"; an empty list means "exclude nothing",
    which is why this cannot be written as `exclude_dirs or DEFAULT`.
    """
    names = DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs
    return frozenset(str(n).strip().lower() for n in names if str(n).strip())
_LANG_SUFFIXES = {
    "cpp": (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx"),
    "go": (".go",),
    "java": (".java",),
    "python": (".py",),
}


def _suffixes_for(language: str) -> tuple[str, ...]:
    return _LANG_SUFFIXES.get(language.lower(), _LANG_SUFFIXES["cpp"])


def _is_test_source_file(path: Path, language: str) -> bool:
    """Recognize conventional test files colocated with production code.

    Directory-only exclusion misses layouts such as Go packages, where
    ``reporter.go`` and ``reporter_test.go`` live side by side. Keep this
    deliberately convention-based and language-specific so a production
    filename that merely contains ``test`` is not removed.
    """
    name = path.name.lower()
    stem = path.stem.lower()
    language = language.lower()
    if language == "go":
        return name.endswith("_test.go")
    if language == "python":
        return name.startswith("test_") or name.endswith("_test.py")
    if language == "java":
        return (
            name.endswith("test.java")
            or name.endswith("tests.java")
            or name.endswith("testcase.java")
        )
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith("_tests")
        or stem.endswith("_unittest")
        or stem.endswith("_unittests")
    )


def _is_excluded(path: str, exclude_dirs=None) -> bool:
    parts = Path(path).parts
    excluded = _exclude_set(exclude_dirs)
    return any(p.lower() in excluded for p in parts)


def _iter_source_files(target: Path, language: str = "cpp", exclude_dirs=None):
    suffixes = _suffixes_for(language)
    if target.is_file():
        if not _is_test_source_file(target, language):
            yield target
        return
    excluded = _exclude_set(exclude_dirs)
    for root, dirs, files in os.walk(target):
        # Pruning `dirs` in place stops os.walk from descending, so an
        # excluded directory costs nothing to skip.
        dirs[:] = [d for d in dirs if d.lower() not in excluded]
        for name in files:
            if Path(name).suffix.lower() not in suffixes:
                continue
            path = Path(root) / name
            if _is_test_source_file(path, language):
                continue
            yield path


# Back-compat alias — the old name is referenced in nothing else
# inside the project, but keep it defined for any external callers.
_iter_cpp_files = _iter_source_files


def run_lizard(
    target: Path,
    lizard_binary: str = "lizard",
    language: str = "cpp",
    exclude_dirs=None,
) -> list[dict]:
    """Invoke the Lizard CLI and return one record per function.

    `--csv` is used rather than Lizard's default tabular output. The
    tabular output repeats every function that trips Lizard's *own*
    warning thresholds (cyclomatic_complexity > 15) in a trailing
    "!!!! Warnings !!!!" section, so scanning it line by line counts
    those functions twice — precisely the functions that carry penalty.
    The CSV transform emits each function exactly once and quotes the
    file and function names, so paths containing spaces parse correctly.

    CSV columns:
        nloc, ccn, token, param, length, location, file, name,
        long_name, start_line, end_line
    """
    if not shutil.which(lizard_binary):
        raise StaticAnalysisError(f"lizard binary not found: {lizard_binary}")
    files = list(_iter_source_files(target, language, exclude_dirs))
    if not files:
        return []

    # Lizard's -f wants a real file path; write the filelist to a
    # tempfile rather than streaming through stdin.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8",
    ) as fh:
        fh.write("\n".join(str(p) for p in files))
        filelist_path = fh.name

    try:
        try:
            proc = subprocess.run(
                [lizard_binary, "-l", language, "--csv", "-f", filelist_path],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise StaticAnalysisError(f"lizard failed: {exc}") from exc
    finally:
        try:
            os.unlink(filelist_path)
        except OSError:
            pass

    if proc.returncode != 0:
        raise StaticAnalysisError(
            f"lizard exited {proc.returncode}: {(proc.stderr or '').strip()}"
        )

    records: list[dict] = []
    for row in csv.reader(io.StringIO(proc.stdout)):
        if len(row) < 11:
            continue
        try:
            records.append({
                "file": row[6],
                "line": int(row[9]),
                "name": row[7],
                "ccn": int(row[1]),
                "nloc": int(row[0]),
                "param": int(row[3]),
            })
        except ValueError:
            # Not a data row (a header or a stray banner line).
            continue
    return records


def _gocognit_batches(files: list[Path]) -> list[list[Path]]:
    """Split a file list into subprocess-safe argument batches.

    Passing the exact files selected by ``_iter_source_files`` keeps the
    cognitive population aligned with Lizard and Duplo, including colocated
    ``*_test.go`` files and configurable excluded directories.  Batching
    avoids the platform argument-length limit on large Go repositories.
    """
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_bytes = 0
    for path in files:
        path_bytes = len(os.fsencode(str(path))) + 1
        if current and (len(current) >= 200 or current_bytes + path_bytes > 96_000):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += path_bytes
    if current:
        batches.append(current)
    return batches


def _parse_gocognit_json(raw: str) -> list[dict]:
    """Parse gocognit's JSON ``[]Stat`` output without guessing fields."""
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise StaticAnalysisError(
            f"gocognit returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise StaticAnalysisError("gocognit JSON output is not an array")

    records: list[dict] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise StaticAnalysisError(
                f"gocognit JSON item {index} is not an object"
            )
        pos = item.get("Pos")
        if not isinstance(pos, dict):
            raise StaticAnalysisError(
                f"gocognit JSON item {index} has no Pos object"
            )
        filename = pos.get("Filename")
        name = item.get("FuncName")
        complexity = item.get("Complexity")
        if not isinstance(filename, str) or not filename or not isinstance(name, str):
            raise StaticAnalysisError(
                f"gocognit JSON item {index} has invalid function identity"
            )
        if isinstance(complexity, bool) or not isinstance(complexity, int):
            raise StaticAnalysisError(
                f"gocognit JSON item {index} has invalid Complexity"
            )
        records.append({
            "file": filename,
            "line": int(pos.get("Line", 0) or 0),
            "name": name,
            "cognitive": complexity,
        })
    return records


def run_cognitive(
    target: Path,
    language: str = "cpp",
    exclude_dirs=None,
    gocognit_binary: str = "gocognit",
) -> dict[tuple[str, str], int]:
    """Map (file, function name) -> cognitive complexity.

    C/C++ uses modified_cognitive_complexity. Go uses gocognit's JSON
    output. Other languages return an empty mapping because no compatible
    tool is configured for them.
    """
    language = language.lower()
    if language not in ("cpp", "go"):
        return {}
    out: dict[tuple[str, str], int] = {}
    files = list(_iter_source_files(target, language, exclude_dirs))
    if not files:
        return out

    if language == "go":
        if not gocognit_binary or not shutil.which(gocognit_binary):
            raise StaticAnalysisError(
                f"gocognit binary not found: {gocognit_binary or '(empty)'}"
            )
        for batch in _gocognit_batches(files):
            try:
                proc = subprocess.run(
                    [gocognit_binary, "-json", *map(str, batch)],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise StaticAnalysisError(f"gocognit failed: {exc}") from exc
            if proc.returncode != 0:
                raise StaticAnalysisError(
                    f"gocognit exited {proc.returncode}: "
                    f"{(proc.stderr or '').strip()}"
                )
            for record in _parse_gocognit_json(proc.stdout):
                out[(record["file"], record["name"])] = record["cognitive"]
        return out

    if not _HAS_COGNITIVE:
        raise StaticAnalysisError(
            "modified_cognitive_complexity is unavailable for C/C++ analysis"
        )
    for path in files:
        try:
            scores = cognitive_complexity_for_file(path)
        except Exception as exc:
            raise StaticAnalysisError(
                f"cognitive complexity failed on {path}: {exc}"
            ) from exc
        for raw_name, score in scores.items():
            if raw_name is None:
                continue
            name = (
                raw_name.decode("utf-8", "replace")
                if isinstance(raw_name, (bytes, bytearray))
                else str(raw_name)
            )
            out[(str(path), name)] = int(score)
    return out


@dataclass
class DuplicationResult:
    """Codebase-level duplication, using the selected tool's accounting.

    `ratio` feeds the duplicate penalty (thesis Eq 4.5). `blocks` is the
    block count Table 4.1 reports next to the line ratio.
    """
    ratio: float = 0.0
    duplicate_lines: int = 0
    total_lines: int = 0
    blocks: int = 0


_DUPLO_DUP_RE = re.compile(r"Duplicate lines of code:\s*(\d+)", re.IGNORECASE)
_DUPLO_TOTAL_RE = re.compile(r"^\s*Lines of code:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_DUPLO_BLOCKS_RE = re.compile(
    r"Total\s+(\d+)\s+duplicate\s+block\(s\)\s+found", re.IGNORECASE
)

_GO_DUPL_LINE_RE = re.compile(
    r"^(.*):(\d+)-(\d+): duplicate of (.*):(\d+)-(\d+)$"
)


def _parse_go_dupl_plumbing(
    output: str,
    file_line_counts: dict[Path, int],
) -> DuplicationResult:
    """Parse ``dupl -plumbing`` and count each physical line once.

    dupl prints every clone pair in both directions. Canonicalising pairs
    prevents double-counting blocks; merging intervals per file prevents
    overlapping clone reports from making the line ratio exceed 100%.
    """
    allowed = {path.resolve(): count for path, count in file_line_counts.items()}
    pairs: set[tuple[tuple[Path, int, int], tuple[Path, int, int]]] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _GO_DUPL_LINE_RE.fullmatch(line)
        if match is None:
            raise StaticAnalysisError(f"unrecognized dupl plumbing output: {line}")
        left = (Path(match.group(1)).resolve(), int(match.group(2)), int(match.group(3)))
        right = (Path(match.group(4)).resolve(), int(match.group(5)), int(match.group(6)))
        for path, start, end in (left, right):
            if path not in allowed:
                raise StaticAnalysisError(f"dupl reported an out-of-scope file: {path}")
            if start < 1 or end < start or end > allowed[path]:
                raise StaticAnalysisError(
                    f"dupl reported an invalid range: {path}:{start}-{end}"
                )
        pairs.add(tuple(sorted((left, right), key=lambda item: (str(item[0]), item[1], item[2]))))

    ranges: dict[Path, list[tuple[int, int]]] = {}
    for pair in pairs:
        for path, start, end in pair:
            ranges.setdefault(path, []).append((start, end))
    duplicate_lines = 0
    for intervals in ranges.values():
        intervals.sort()
        merged_start, merged_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= merged_end + 1:
                merged_end = max(merged_end, end)
            else:
                duplicate_lines += merged_end - merged_start + 1
                merged_start, merged_end = start, end
        duplicate_lines += merged_end - merged_start + 1
    total_lines = sum(allowed.values())
    return DuplicationResult(
        ratio=(duplicate_lines / total_lines) if total_lines else 0.0,
        duplicate_lines=duplicate_lines,
        total_lines=total_lines,
        blocks=len(pairs),
    )


def run_go_dupl(
    target: Path,
    dupl_binary: str,
    threshold_tokens: int = 100,
    exclude_dirs=None,
) -> DuplicationResult:
    """Measure Go duplication with mibk/dupl's AST-based detector.

    The denominator is physical lines in the exact production-file list
    supplied to dupl. Clone ranges are physical source lines too, keeping
    the ratio dimensionally consistent and bounded to [0, 1].
    """
    if not dupl_binary:
        return DuplicationResult()
    if not shutil.which(dupl_binary):
        raise StaticAnalysisError(f"dupl binary not found: {dupl_binary}")
    files = [path.resolve() for path in _iter_source_files(target, "go", exclude_dirs)]
    if not files:
        return DuplicationResult()
    file_line_counts = {
        path: len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        for path in files
    }
    try:
        proc = subprocess.run(
            [dupl_binary, "-files", "-plumbing", "-t", str(threshold_tokens)],
            input="\n".join(map(str, files)),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise StaticAnalysisError(f"dupl failed: {exc}") from exc
    if proc.returncode != 0:
        raise StaticAnalysisError(
            f"dupl exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()}"
        )
    return _parse_go_dupl_plumbing(proc.stdout, file_line_counts)


def run_duplo(
    target: Path,
    duplo_binary: str,
    min_block_lines: int = 4,
    language: str = "cpp",
    exclude_dirs=None,
) -> DuplicationResult:
    """Measure codebase duplication with Duplo.

    Duplo's text summary is parsed rather than its `-json` output:

        Lines of code: N
        Duplicate lines of code: N
        Total N duplicate block(s) found.

    The thesis (§4.2.2) notes Duplo was selected partly for its JSON
    output, but that output contains only the block list — it carries no
    line totals, so the duplicate-line *ratio* cannot be derived from it.
    Duplo's own "Lines of code" figure is its post-filter count (it drops
    preprocessor directives under -ip and lines below the -mc minimum),
    which cannot be reconstructed externally. Parsing the summary is
    therefore the only way to reproduce the ratio Duplo itself reports,
    and hence the 2.41% -> 19.4 calibration in §4.5.1. The block count
    the JSON would provide is in the summary too.

    `-ip` matches §4.2.2's requirement that preprocessor directives be
    filtered out before analysis; Duplo strips comments natively.

    `min_block_lines` defaults to 4, which is Duplo's own default for
    `-ml`. The thesis never specifies a minimum block size — §4.2.2 only
    requires that preprocessor directives and comments be filtered — so
    the tool's default is the reconstruction that assumes least. Every
    other Duplo knob (`-pt`, `-mc`, `-d`) is likewise left at its default.
    Raising it suppresses short duplicate blocks, which lowers both the
    ratio and the block count; Table 4.1's 7 blocks at 2.41% is the
    figure to calibrate against on the real target codebase.
    """
    if not duplo_binary:
        return DuplicationResult()
    if not shutil.which(duplo_binary):
        raise StaticAnalysisError(f"duplo binary not found: {duplo_binary}")
    files = list(_iter_source_files(target, language, exclude_dirs))
    if not files:
        return DuplicationResult()
    # Duplo silently reports zero lines for relative paths in its file
    # list, so the paths handed to it must be absolute.
    file_list = "\n".join(str(p.resolve()) for p in files)
    try:
        proc = subprocess.run(
            [duplo_binary, "-ml", str(min_block_lines), "-ip", "-", "-"],
            input=file_list,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise StaticAnalysisError(f"duplo failed: {exc}") from exc
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    dup = _DUPLO_DUP_RE.search(output)
    total = _DUPLO_TOTAL_RE.search(output)
    blocks = _DUPLO_BLOCKS_RE.search(output)
    if not dup or not total:
        raise StaticAnalysisError(
            f"duplo exited {proc.returncode} without line totals: {output.strip()}"
        )
    duplicate_lines = int(dup.group(1))
    total_lines = int(total.group(1))
    return DuplicationResult(
        ratio=(duplicate_lines / total_lines) if total_lines else 0.0,
        duplicate_lines=duplicate_lines,
        total_lines=total_lines,
        blocks=int(blocks.group(1)) if blocks else 0,
    )


def _function_match_keys(name: str) -> set[str]:
    """Possible match keys for joining Lizard and cognitive records.

    Kept available for debug/inspection purposes — the penalty function
    no longer relies on it (cognitive records are penalized as their
    own list).
    """
    keys = {name}
    if "::" in name:
        keys.add(name.rsplit("::", 1)[-1])
    return keys


def run_static_analysis(
    target: Path,
    thresholds: dict,
    duplo_binary: str = "",
    duplo_min_block_lines: int = 4,
    dupl_binary: str = "",
    dupl_threshold_tokens: int = 100,
    lizard_binary: str = "lizard",
    gocognit_binary: str = "gocognit",
    language: str = "cpp",
    exclude_dirs=None,
) -> tuple[list[dict], list[dict], DuplicationResult]:
    """Return (lizard_records, cognitive_records, duplication).

    Thresholds is accepted for signature symmetry; tools.py itself does
    no thresholding (penalty.py applies that).

    All three tools receive the same file list, so `exclude_dirs` cannot
    drift between the per-function metrics and the duplication ratio.
    """
    lizard_records = run_lizard(
        target, lizard_binary=lizard_binary, language=language,
        exclude_dirs=exclude_dirs,
    )
    cog_map = run_cognitive(
        target,
        language=language,
        exclude_dirs=exclude_dirs,
        gocognit_binary=gocognit_binary,
    )

    lizard_lines = {
        (record["file"], record["name"]): int(record.get("line", 0) or 0)
        for record in lizard_records
    }
    cognitive_records = [
        {
            "file": file_,
            "line": lizard_lines.get((file_, name), 0),
            "name": name,
            "cognitive": score,
        }
        for (file_, name), score in cog_map.items()
    ]

    # Optional join for debug/inspection — does not change penalty math.
    if cog_map:
        for rec in lizard_records:
            for nm in _function_match_keys(rec["name"]):
                if (rec["file"], nm) in cog_map:
                    rec["cognitive"] = cog_map[(rec["file"], nm)]
                    break

    if language.lower() == "go":
        duplication = run_go_dupl(
            target, dupl_binary, dupl_threshold_tokens,
            exclude_dirs=exclude_dirs,
        )
    else:
        duplication = run_duplo(
            target, duplo_binary, duplo_min_block_lines, language=language,
            exclude_dirs=exclude_dirs,
        )
    return lizard_records, cognitive_records, duplication
