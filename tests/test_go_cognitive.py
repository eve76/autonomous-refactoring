#!/usr/bin/env python3
"""Regression checks for Go cognitive-complexity measurement."""

import json
import tempfile
from pathlib import Path
import sys

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from analysis.penalty import compute_total_penalty  # noqa: E402
from analysis.tools import (                         # noqa: E402
    StaticAnalysisError,
    run_cognitive,
    run_static_analysis,
)


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


FAKE_GOCOGNIT = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

files = [pathlib.Path(value) for value in sys.argv[1:] if value != "-json"]
if any(path.name.endswith("_test.go") or "tests" in path.parts for path in files):
    print("excluded test source was passed", file=sys.stderr)
    raise SystemExit(9)
print(json.dumps([
    {
        "PkgName": "fixture",
        "FuncName": path.stem.title(),
        "Complexity": 17 if path.name == "main.go" else 4,
        "Pos": {
            "Filename": str(path),
            "Offset": 0,
            "Line": 3,
            "Column": 1,
        },
    }
    for path in files
]))
'''


FAKE_LIZARD = r'''#!/usr/bin/env python3
import pathlib
import sys

filelist = pathlib.Path(sys.argv[sys.argv.index("-f") + 1])
for raw in filelist.read_text().splitlines():
    path = pathlib.Path(raw)
    name = path.stem.title()
    print(f'10,2,20,1,12,{name}@3-14@{path},{path},{name},{name},3,14')
'''


def executable(path: Path, content: str) -> str:
    path.write_text(content)
    path.chmod(path.stat().st_mode | 0o111)
    return str(path)


print("\n[1] gocognit JSON is parsed over the exact production Go file set")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    source = root / "source"
    source.mkdir()
    (source / "main.go").write_text("package fixture\n\nfunc Main() {}\n")
    (source / "helper.go").write_text("package fixture\n\nfunc Helper() {}\n")
    (source / "main_test.go").write_text("package fixture\n\nfunc TestMain() {}\n")
    tests = source / "tests"
    tests.mkdir()
    (tests / "ignored.go").write_text("package tests\n\nfunc Ignored() {}\n")

    gocognit = executable(root / "gocognit", FAKE_GOCOGNIT)
    records = run_cognitive(
        source, language="go", gocognit_binary=gocognit,
    )
    check("production functions are measured",
          len(records) == 2 and sorted(records.values()) == [4, 17], str(records))
    check("colocated and directory tests are excluded",
          all("test" not in Path(file_).name for file_, _ in records))

    lizard = executable(root / "lizard", FAKE_LIZARD)
    lizard_records, cognitive_records, duplication = run_static_analysis(
        source,
        {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5},
        lizard_binary=lizard,
        gocognit_binary=gocognit,
        language="go",
    )
    check("static-analysis pipeline keeps cognitive as an independent population",
          len(lizard_records) == 2 and len(cognitive_records) == 2)
    check("Go cognitive values contribute to the normal penalty",
          compute_total_penalty(
              lizard_records,
              cognitive_records,
              {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5},
              duplication.ratio,
              {"ccn": 0, "cognitive": 1, "nloc": 0, "param": 0, "duplicates": 0},
          ) > 0)


print("\n[2] missing and malformed gocognit executions fail closed")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    source = root / "source.go"
    source.write_text("package fixture\n\nfunc Source() {}\n")
    try:
        run_cognitive(
            source, language="go",
            gocognit_binary=str(root / "missing-gocognit"),
        )
    except StaticAnalysisError as exc:
        check("missing binary is explicit", "binary not found" in str(exc))
    else:
        check("missing binary is explicit", False)

    malformed = executable(
        root / "bad-gocognit",
        "#!/bin/sh\nprintf 'not-json\\n'\n",
    )
    try:
        run_cognitive(source, language="go", gocognit_binary=malformed)
    except StaticAnalysisError as exc:
        check("malformed JSON is explicit", "invalid JSON" in str(exc))
    else:
        check("malformed JSON is explicit", False)


print("\n" + ("ALL GO COGNITIVE CHECKS PASSED" if not FAILURES else "FAILED: " + ", ".join(FAILURES)))
raise SystemExit(1 if FAILURES else 0)
