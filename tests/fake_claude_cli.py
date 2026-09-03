#!/usr/bin/env python3
"""Deterministic Claude-CLI stand-in for the full coordinator E2E test.

It accepts the subset of Claude Code flags used by agent_runner.py, reads the
task prompt on stdin, and emits Claude stream-json. Analysts report one of the
controlled FerretDB fixture files. Programmers perform a behavior-preserving
refactoring, commit it, and invoke the real merge-gate CLI.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


FUNCTION_NAMES = {
    1: "QualityOne",
    2: "QualityTwo",
    3: "QualityThree",
}


def emit(text: str, role: str = "unknown") -> None:
    # Deterministic aggregate usage lets the E2E verify accounting without
    # contacting a paid provider.
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


def emit_structured(payload: dict) -> None:
    print(json.dumps({
        "type": "result",
        "result": json.dumps(payload, sort_keys=True),
        "structured_output": payload,
        "num_turns": 1,
        "total_cost_usd": 0.005,
        "usage": {"input_tokens": 300, "output_tokens": 30},
        "modelUsage": {
            "claude-opus-4-7": {
                "inputTokens": 300,
                "cacheCreationInputTokens": 0,
                "cacheReadInputTokens": 0,
                "outputTokens": 30,
            },
        },
    }), flush=True)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), text=True, capture_output=True,
    )


def good_source(index: int) -> str:
    fn = FUNCTION_NAMES[index]
    helper = fn[0].lower() + fn[1:]
    chunks = []
    for band in range(4):
        start = band * 5 + 1
        conditions = "\n".join(
            f"\tif v > {value} {{\n\t\tresult++\n\t}}"
            for value in range(start, start + 5)
        )
        chunks.append(
            f"func {helper}Band{band}(v int) int {{\n"
            f"\tresult := 0\n{conditions}\n\treturn result\n}}\n"
        )
    calls = " + ".join(f"{helper}Band{band}(v)" for band in range(4))
    return (
        "package e2efixture\n\n"
        + "\n".join(chunks)
        + f"\nfunc {fn}(v int) int {{\n\treturn {calls}\n}}\n"
    )


def analyst(task_prompt: str) -> int:
    match = re.search(r"quality_([123])\.go", task_prompt)
    indices = [int(match.group(1))] if match else [1, 2, 3]
    lines = []
    for index in indices:
        fn = FUNCTION_NAMES[index]
        lines.append(
            f"ISSUE: quality_{index}.go:3 - high - complexity - "
            f"Function {fn} has CCN=21, 64 NLOC"
        )
    emit("\n".join(lines), "analyst")
    return 0


def programmer(task_prompt: str, cwd: Path) -> int:
    cfg_match = re.search(r"--config\s+([^\s]+)", task_prompt)
    branch_match = re.search(
        r"integration branch for this run is `([^`]+)`", task_prompt,
    )
    issue_matches = re.findall(
        r"(ISSUE-\d+).*?(quality_([123])\.go):\d+", task_prompt,
    )
    if not cfg_match or not branch_match or not issue_matches:
        emit(
            "RESULT: ISSUE-UNKNOWN - skipped - fake CLI could not parse task",
            "programmer",
        )
        return 1

    config_path = Path(cfg_match.group(1))
    cfg = json.loads(config_path.read_text())
    branch = branch_match.group(1)
    results = []

    for issue_id, relative_file, raw_index in issue_matches:
        index = int(raw_index)
        reset = run(["git", "reset", "--hard", branch], cwd)
        clean = run(["git", "clean", "-fd"], cwd)
        if reset.returncode or clean.returncode:
            results.append(
                f"RESULT: {issue_id} - skipped - failed to reset worktree"
            )
            continue

        source = cwd / cfg["target_subdir"] / relative_file
        source.write_text(good_source(index))
        add = run(["git", "add", str(source)], cwd)
        commit = run(
            ["git", "commit", "-m", f"refactor {FUNCTION_NAMES[index]}"],
            cwd,
        )
        if add.returncode or commit.returncode:
            results.append(
                f"RESULT: {issue_id} - skipped - failed to commit refactoring"
            )
            continue

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
            results.append(
                f"RESULT: {issue_id} - done - merged at penalty "
                f"{verdict['penalty_before']} -> {verdict['penalty_after']}"
            )
        else:
            reason = (verdict or {}).get("reason") or gate.stderr.strip() or "gate failed"
            results.append(f"RESULT: {issue_id} - skipped - {reason}")

    emit("\n".join(results), "programmer")
    return 0


def main() -> int:
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        }))
        return 0
    if "--version" in sys.argv[1:]:
        print("2.1.209 (Claude Code fake)")
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--append-system-prompt", default="")
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--permission-mode")
    parser.add_argument("--output-format")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--safe-mode", action="store_true")
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--allowedTools")
    parser.add_argument("--tools")
    parser.add_argument("--json-schema")
    parser.add_argument("--max-turns")
    parser.add_argument("--disable-slash-commands", action="store_true")
    args, _ = parser.parse_known_args()
    task_prompt = sys.stdin.read()
    if "code-quality analyst" in args.append_system_prompt:
        return analyst(task_prompt)
    if "programmer agent" in args.append_system_prompt:
        return programmer(task_prompt, Path.cwd())
    if "deterministic decision component" in args.system_prompt:
        if "STUCK PROGRAMMERS" in task_prompt:
            emit_structured({
                "terminate": [], "keep": [], "infeasible_issues": [],
                "reasoning": "fake subscription orchestrator",
            })
        else:
            emit_structured({
                "programmer_assignments": [], "analyst_assignments": [],
                "reasoning": "fake subscription orchestrator",
            })
        return 0
    emit("unsupported fake role", "unknown")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
