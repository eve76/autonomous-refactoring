"""Wrappers around the static-analysis tools used by the multi-agent
system (gnomad-kiro parity).

Lizard is invoked as a CLI subprocess against a file list (`-l cpp -f`).
Cognitive complexity uses the modified_cognitive_complexity package.
Duplo is also a CLI subprocess; its text summary is parsed for the
duplicate-line ratio. Per-function records are returned as two
separate lists (lizard and cognitive) — they are combined later by
the penalty function, not joined here.

run_static_analysis returns (lizard_records, cognitive_records, dup_ratio).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from modified_cognitive_complexity import cognitive_complexity_for_file
    _HAS_COGNITIVE = True
except ImportError:
    _HAS_COGNITIVE = False


# gnomad-kiro only excludes a `test` directory (case-insensitive).
_DEFAULT_EXCLUDE = ("test",)
_LANG_SUFFIXES = {
    "cpp": (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx"),
    "go": (".go",),
    "java": (".java",),
    "python": (".py",),
}


def _suffixes_for(language: str) -> tuple[str, ...]:
    return _LANG_SUFFIXES.get(language.lower(), _LANG_SUFFIXES["cpp"])


def _is_excluded(path: str) -> bool:
    parts = Path(path).parts
    return any(p.lower() in _DEFAULT_EXCLUDE for p in parts)


def _iter_source_files(target: Path, language: str = "cpp"):
    suffixes = _suffixes_for(language)
    if target.is_file():
        yield target
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d.lower() not in _DEFAULT_EXCLUDE]
        for name in files:
            if Path(name).suffix.lower() not in suffixes:
                continue
            yield Path(root) / name


# Back-compat alias — the old name is referenced in nothing else
# inside the project, but keep it defined for any external callers.
_iter_cpp_files = _iter_source_files


# Lizard CLI per-function row:
#     NLOC CCN token PARAM length name@start-end@file
_LIZARD_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)@(\d+)-\d+@(\S+)\s*$"
)


def run_lizard(
    target: Path,
    lizard_binary: str = "lizard",
    language: str = "cpp",
) -> list[dict]:
    """Invoke the Lizard CLI on a list of source files and parse stdout.

    Output format (one row per function):
        NLOC CCN token PARAM length name@start-end@file
    """
    if not shutil.which(lizard_binary):
        print(f"[tools] lizard binary not found: {lizard_binary}", file=sys.stderr)
        return []
    files = list(_iter_source_files(target, language))
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
                [lizard_binary, "-l", language, "-f", filelist_path],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[tools] lizard failed: {exc}", file=sys.stderr)
            return []
    finally:
        try:
            os.unlink(filelist_path)
        except OSError:
            pass

    records: list[dict] = []
    for raw in proc.stdout.splitlines():
        m = _LIZARD_LINE_RE.match(raw)
        if not m:
            continue
        nloc, ccn, _tok, param, _length, name, start, file_ = m.groups()
        records.append({
            "file": file_,
            "line": int(start),
            "name": name,
            "ccn": int(ccn),
            "nloc": int(nloc),
            "param": int(param),
        })
    return records


def run_cognitive(target: Path, language: str = "cpp") -> dict[tuple[str, str], int]:
    """Map (file, function name) -> cognitive complexity.

    modified_cognitive_complexity supports C/C++ only; for other
    languages the function returns an empty mapping.
    """
    if not _HAS_COGNITIVE or language.lower() != "cpp":
        return {}
    out: dict[tuple[str, str], int] = {}
    for path in _iter_source_files(target, language):
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


_DUPLO_DUP_RE = re.compile(r"Duplicate lines of code:\s*(\d+)", re.IGNORECASE)
_DUPLO_TOTAL_RE = re.compile(r"^\s*Lines of code:\s*(\d+)", re.IGNORECASE | re.MULTILINE)


def run_duplo(
    target: Path,
    duplo_binary: str,
    min_block_lines: int = 10,
    language: str = "cpp",
) -> float:
    """Return duplicate-line ratio for the codebase, via Duplo.

    Parses Duplo's text summary lines (gnomad-kiro parity):
        Duplicate lines of code: N
        Lines of code: N
    Note: Duplo counts each duplicate occurrence, so a single block
    found in K files contributes K * lines to the numerator. The ratio
    can therefore exceed 1.0. The penalty function (Eq 4.5) saturates
    near 100 either way; this matches gnomad-kiro behavior.
    """
    if not duplo_binary or not shutil.which(duplo_binary):
        return 0.0
    files = list(_iter_source_files(target, language))
    if not files:
        return 0.0
    file_list = "\n".join(str(p) for p in files)
    try:
        proc = subprocess.run(
            [duplo_binary, "-ml", str(min_block_lines), "-ip", "-", "-"],
            input=file_list,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[tools] duplo failed: {exc}", file=sys.stderr)
        return 0.0
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    dup = _DUPLO_DUP_RE.search(output)
    total = _DUPLO_TOTAL_RE.search(output)
    if not dup or not total:
        return 0.0
    total_lines = int(total.group(1))
    if total_lines == 0:
        return 0.0
    return int(dup.group(1)) / total_lines


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
    duplo_min_block_lines: int = 10,
    lizard_binary: str = "lizard",
    language: str = "cpp",
) -> tuple[list[dict], list[dict], float]:
    """Return (lizard_records, cognitive_records, dup_ratio).

    Thresholds is accepted for signature symmetry; tools.py itself does
    no thresholding (penalty.py applies that).
    """
    lizard_records = run_lizard(target, lizard_binary=lizard_binary, language=language)
    cog_map = run_cognitive(target, language=language)

    cognitive_records = [
        {"file": file_, "name": name, "cognitive": score}
        for (file_, name), score in cog_map.items()
    ]

    # Optional join for debug/inspection — does not change penalty math.
    if cog_map:
        for rec in lizard_records:
            for nm in _function_match_keys(rec["name"]):
                if (rec["file"], nm) in cog_map:
                    rec["cognitive"] = cog_map[(rec["file"], nm)]
                    break

    dup_ratio = run_duplo(target, duplo_binary, duplo_min_block_lines, language=language)
    return lizard_records, cognitive_records, dup_ratio
