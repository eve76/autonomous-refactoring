#!/usr/bin/env python3
"""Deterministic CLI stand-in that refactors one real file per target repo.

The companion ``test_real_repo_fake_cli_e2e.py`` selects a scenario through
``FAKE_REPO_SCENARIO``.  Analysts emit one controlled issue and programmers
perform a small behavior-preserving extraction, commit it, and invoke the
normal merge-gate CLI.  No paid API is contacted.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SCENARIOS = {
    "mongo_query_multikey": {
        "file": "multikey_dotted_path_support.cpp",
        "repo_file": "src/mongo/db/query/bson/multikey_dotted_path_support.cpp",
        "line": 55,
        "function": "_extractAllElementsAlongPath",
        "metric": "CCN=19, 69 NLOC",
        "helper_marker": "bool _isNumericPathComponent",
    },
    "ferret_telemetry": {
        "file": "telemetry.go",
        "repo_file": "internal/util/telemetry/telemetry.go",
        "line": 73,
        "function": "initialState",
        "metric": "42 NLOC",
        "helper_marker": "func configuredState",
    },
}


def emit(text: str, role: str) -> None:
    usage = {
        "input_tokens": 1000 if role == "analyst" else 2000,
        "cache_creation_input_tokens": 100 if role == "analyst" else 250,
        "cache_read_input_tokens": 200 if role == "analyst" else 500,
        "output_tokens": 100 if role == "analyst" else 200,
    }
    print(json.dumps({
        "type": "result",
        "result": text,
        "num_turns": 4 if role == "analyst" else 8,
        "total_cost_usd": 0.01 if role == "analyst" else 0.02,
        "usage": usage,
    }), flush=True)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), text=True, capture_output=True,
    )


def scenario() -> tuple[str, dict]:
    name = os.environ.get("FAKE_REPO_SCENARIO", "")
    try:
        return name, SCENARIOS[name]
    except KeyError:
        raise RuntimeError(
            f"unknown FAKE_REPO_SCENARIO {name!r}; "
            f"choose one of {', '.join(SCENARIOS)}"
        ) from None


def analyst() -> int:
    _, spec = scenario()
    source = Path(spec["file"])
    if source.exists() and spec["helper_marker"] in source.read_text():
        emit("", "analyst")
        return 0
    emit(
        f"ISSUE: {spec['file']}:{spec['line']} - high - complexity - "
        f"Function {spec['function']} has {spec['metric']}",
        "analyst",
    )
    return 0


def refactor_mongo(source: Path) -> None:
    before = '''            bool allDigits = false;
            if (next.size() > 0 && ctype::isDigit(next[0])) {
                unsigned temp = 1;
                while (temp < next.size() && ctype::isDigit(next[temp]))
                    temp++;
                allDigits = temp == next.size() || next[temp] == '.';
            }
            if (allDigits) {
'''
    after = '''            if (_isNumericPathComponent(next)) {
'''
    helper = '''bool _isNumericPathComponent(StringData path) {
    bool allDigits = false;
    if (path.size() > 0 && ctype::isDigit(path[0])) {
        unsigned temp = 1;
        while (temp < path.size() && ctype::isDigit(path[temp])) {
            temp++;
        }
        allDigits = temp == path.size() || path[temp] == '.';
    }
    return allDigits;
}

'''
    marker = "template <typename BSONElementColl>\n"
    text = source.read_text()
    if text.count(before) != 1 or text.count(marker) != 1:
        raise RuntimeError("MongoDB query source no longer matches fixture")
    text = text.replace(before, after)
    source.write_text(text.replace(marker, helper + marker))


def refactor_ferret(source: Path) -> None:
    before = '''\t// if flag is unset, use previous unlocked state
\tif f.v == nil {
\t\tstate = prev

\t\tif state == nil {
\t\t\t// undecided state, reporter would log about it during run
\t\t\treturn
\t\t}

\t\tif *state {
\t\t\tl.Info("Telemetry is enabled because it was enabled previously")
\t\t} else {
\t\t\tl.Info("Telemetry is disabled because it was disabled previously")
\t\t}

\t\treturn
\t}

\t// flag is set, use it as locked state
\tstate = f.v
\tlocked = true

\tif *state {
\t\tl.Info("Telemetry enabled")
\t} else {
\t\tl.Info("Telemetry disabled")
\t}

\treturn
'''
    after = '''\tstate, locked = configuredState(f, prev, l)
\treturn
'''
    helper = '''
func configuredState(f *Flag, prev *bool, l *slog.Logger) (state *bool, locked bool) {
\t// if flag is unset, use previous unlocked state
\tif f.v == nil {
\t\tstate = prev
\t\tif state == nil {
\t\t\t// undecided state, reporter would log about it during run
\t\t\treturn
\t\t}

\t\tif *state {
\t\t\tl.Info("Telemetry is enabled because it was enabled previously")
\t\t} else {
\t\t\tl.Info("Telemetry is disabled because it was disabled previously")
\t\t}
\t\treturn
\t}

\t// flag is set, use it as locked state
\tstate = f.v
\tlocked = true
\tif *state {
\t\tl.Info("Telemetry enabled")
\t} else {
\t\tl.Info("Telemetry disabled")
\t}
\treturn
}

'''
    marker = "// check interfaces\n"
    text = source.read_text()
    if text.count(before) != 1 or text.count(marker) != 1:
        raise RuntimeError("FerretDB initialState source no longer matches fixture")
    text = text.replace(before, after)
    source.write_text(text.replace(marker, helper + marker))
    formatted = run(["gofmt", "-w", str(source)], source.parent)
    if formatted.returncode:
        raise RuntimeError(formatted.stderr or "gofmt failed")


def apply_refactoring(name: str, source: Path) -> None:
    if name == "mongo_query_multikey":
        refactor_mongo(source)
    elif name == "ferret_telemetry":
        refactor_ferret(source)
    else:
        raise RuntimeError(f"unsupported scenario {name}")


def programmer(task_prompt: str, cwd: Path) -> int:
    name, spec = scenario()
    cfg_match = re.search(r"--config\s+([^\s]+)", task_prompt)
    branch_match = re.search(
        r"integration branch for this run is `([^`]+)`", task_prompt,
    )
    issue_match = re.search(r"(ISSUE-\d+).*?" + re.escape(spec["file"]), task_prompt)
    if not cfg_match or not branch_match or not issue_match:
        emit(
            "RESULT: ISSUE-UNKNOWN - skipped - fake CLI could not parse task",
            "programmer",
        )
        return 1

    issue_id = issue_match.group(1)
    config_path = Path(cfg_match.group(1))
    cfg = json.loads(config_path.read_text())
    branch = branch_match.group(1)

    reset = run(["git", "reset", "--hard", branch], cwd)
    clean = run(["git", "clean", "-fd"], cwd)
    if reset.returncode or clean.returncode:
        emit(
            f"RESULT: {issue_id} - skipped - failed to reset worktree",
            "programmer",
        )
        return 0

    try:
        source = cwd / cfg["target_subdir"] / spec["file"]
        apply_refactoring(name, source)
    except Exception as exc:
        emit(f"RESULT: {issue_id} - skipped - {exc}", "programmer")
        return 0

    add = run(["git", "add", spec["repo_file"]], cwd)
    commit = run(
        ["git", "commit", "-m", f"refactor {spec['function']}"], cwd,
    )
    if add.returncode or commit.returncode:
        emit(
            f"RESULT: {issue_id} - skipped - failed to commit refactoring",
            "programmer",
        )
        return 0

    gate = run(
        [
            cfg["gate_python"],
            cfg["gate_cli"],
            "--config",
            str(config_path),
            "--issue-id",
            issue_id,
        ],
        cwd,
    )
    verdict = None
    for line in reversed(gate.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "success" in candidate:
            verdict = candidate
            break

    if verdict and verdict.get("success"):
        emit(
            f"RESULT: {issue_id} - done - merged at penalty "
            f"{verdict['penalty_before']} -> {verdict['penalty_after']}",
            "programmer",
        )
    else:
        reason = (verdict or {}).get("reason") or gate.stderr.strip() or "gate failed"
        emit(f"RESULT: {issue_id} - skipped - {reason}", "programmer")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--append-system-prompt", default="")
    parser.add_argument("--permission-mode")
    parser.add_argument("--output-format")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--allowedTools")
    args, _ = parser.parse_known_args()
    task_prompt = sys.stdin.read()
    try:
        if "code-quality analyst" in args.append_system_prompt:
            return analyst()
        if "programmer agent" in args.append_system_prompt:
            return programmer(task_prompt, Path.cwd())
    except Exception as exc:
        emit(f"fake CLI error: {exc}", "unknown")
        return 2
    emit("unsupported fake role", "unknown")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
