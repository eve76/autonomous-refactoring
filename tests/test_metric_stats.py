"""Verify the per-metric distribution statistics (thesis Tables 4.1 / 5.1).

The thesis publishes its own baseline distribution in Table 4.1, so the
statistics code is checked by reconstructing a synthetic population whose
Table 4.1 row is known, and by exercising Duplo end-to-end for the
duplicate line ratio and block count.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from analysis.metrics import (                                          # noqa: E402
    compute_metric_stats, format_metric_stats, mean_change_pct,
    percentile, PERCENTILE_METHOD,
)
from analysis.tools import (                                            # noqa: E402
    DuplicationResult, _parse_go_dupl_plumbing, run_duplo, run_go_dupl,
)

THRESHOLDS = {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5, "duplicates": 0}

FAILURES = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def lz(**kw):
    """One Lizard record."""
    base = {"file": "f.cc", "line": 1, "name": "fn"}
    base.update(kw)
    return base


def main() -> int:
    print("\n[1] percentile helper")
    ordered = [float(i) for i in range(1, 101)]      # 1..100
    check("p50 of 1..100", abs(percentile(ordered, 50) - 50.5) < 1e-9,
          f"{percentile(ordered, 50)}")
    check("p90 of 1..100", abs(percentile(ordered, 90) - 90.1) < 1e-9,
          f"{percentile(ordered, 90)}")
    check("p100 == max", percentile(ordered, 100) == 100.0)
    check("empty population is 0", percentile([], 90) == 0.0)
    check("single value", percentile([7.0], 90) == 7.0)
    check("method is documented", bool(PERCENTILE_METHOD), PERCENTILE_METHOD)

    print("\n[2] statistics are taken over ALL functions, not only violators")
    # Table 4.1 reasoning: mean CCN 3.21 against threshold 15 only makes
    # sense over the whole population.
    records = [lz(ccn=1, nloc=5, param=0) for _ in range(9)] + [lz(ccn=41, nloc=90, param=7)]
    stats = compute_metric_stats(records, [], DuplicationResult(), THRESHOLDS)
    check("population counts every function", stats["ccn"]["functions"] == 10)
    check("mean over all functions", abs(stats["ccn"]["mean"] - 5.0) < 1e-9,
          f"{stats['ccn']['mean']}")
    check("median unaffected by the single outlier", stats["ccn"]["median"] == 1.0)
    check("max is the outlier", stats["ccn"]["max"] == 41.0)
    check("only the outlier is over threshold", stats["ccn"]["over_threshold"] == 1)
    check("ccn not cleared", stats["ccn"]["cleared"] is False)
    check("param over threshold counted", stats["param"]["over_threshold"] == 1,
          f"max={stats['param']['max']}")

    print("\n[3] 'cleared' means every function is below the threshold (§4.5.1)")
    clean = [lz(ccn=3, nloc=10, param=2) for _ in range(5)]
    cstats = compute_metric_stats(clean, [], DuplicationResult(), THRESHOLDS)
    check("ccn cleared", cstats["ccn"]["cleared"] is True)
    check("nloc cleared", cstats["nloc"]["cleared"] is True)
    check("param cleared", cstats["param"]["cleared"] is True)
    check("no duplicate blocks counts as cleared",
          cstats["duplicates"]["cleared"] is True)

    print("\n[4] cognitive comes from its OWN population, not the Lizard list")
    # Table 4.1 reports cognitive mean 1.32, median 0, max 19: the tool
    # scores every function it parses, so most entries are zero and the
    # mean is pulled up by a few outliers. Reproduce that skew.
    values = [0] * 30 + [4, 6, 16, 19]
    cog = [{"file": "f.cc", "name": f"fn{i}", "cognitive": v}
           for i, v in enumerate(values)]
    s = compute_metric_stats([lz(ccn=1, nloc=5, param=1)], cog, DuplicationResult(),
                             THRESHOLDS)
    check("cognitive population is independent of Lizard's",
          s["cognitive"]["functions"] == 34 and s["ccn"]["functions"] == 1,
          f"cog={s['cognitive']['functions']} ccn={s['ccn']['functions']}")
    check("cognitive median is 0, as in Table 4.1", s["cognitive"]["median"] == 0.0)
    check("cognitive mean 1.32, as in Table 4.1",
          abs(s["cognitive"]["mean"] - 1.32) < 0.01, f"{s['cognitive']['mean']}")
    check("cognitive max is 19, as in Table 4.1", s["cognitive"]["max"] == 19.0)
    check("two cognitive violations (16, 19)", s["cognitive"]["over_threshold"] == 2)

    print("\n[5] Table 5.1 mean-change percentages")
    before = compute_metric_stats(
        [lz(ccn=10, nloc=20, param=2), lz(ccn=30, nloc=60, param=4)], [],
        DuplicationResult(ratio=0.0241, duplicate_lines=241, total_lines=10000, blocks=7),
        THRESHOLDS)
    after = compute_metric_stats(
        [lz(ccn=5, nloc=15, param=2), lz(ccn=15, nloc=45, param=4)], [],
        DuplicationResult(ratio=0.0032, duplicate_lines=32, total_lines=10000, blocks=1),
        THRESHOLDS)
    change = mean_change_pct(before, after)
    check("ccn mean 20 -> 10 is -50%", change["ccn"] == -50.0, str(change["ccn"]))
    check("nloc mean 40 -> 30 is -25%", change["nloc"] == -25.0, str(change["nloc"]))
    check("param unchanged is 0%", change["param"] == 0.0, str(change["param"]))
    check("duplicates 2.41% -> 0.32% is -86.7% (matches Table 5.1 magnitude)",
          abs(change["duplicates"] - (-86.72)) < 0.02, str(change["duplicates"]))
    check("a zero baseline yields None rather than dividing by zero",
          mean_change_pct(
              compute_metric_stats([], [], DuplicationResult(), THRESHOLDS), after
          )["ccn"] is None)

    print("\n[6] duplication reporting matches Duplo's own accounting")
    duplo = EXP / "bin" / "duplo"
    if not duplo.exists():
        print("      SKIP: bin/duplo not present")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = "\n".join(f"    int v{i} = {i} * 3 + 1;" for i in range(1, 16))
            src = f"#include <cstdio>\nvoid blockA() {{\n{body}\n}}\n"
            (root / "a.cpp").write_text(src)
            (root / "b.cpp").write_text(src.replace("blockA", "blockB"))
            d = run_duplo(root, str(duplo), 10, "cpp")
            print(f"      duplo -> ratio={d.ratio:.4f} "
                  f"dup={d.duplicate_lines} total={d.total_lines} blocks={d.blocks}")
            check("block count captured (Table 4.1 reports it)", d.blocks == 1,
                  str(d.blocks))
            check("duplicate lines equal the block's LineCount (not 2x)",
                  d.duplicate_lines == 15, str(d.duplicate_lines))
            check("total lines come from Duplo's own post-filter count",
                  d.total_lines == 32, str(d.total_lines))
            check("ratio is duplicate/total", abs(d.ratio - 15 / 32) < 1e-9)
            check("ratio is a fraction, not a percentage", d.ratio < 1.0)

            dstats = compute_metric_stats([], [], d, THRESHOLDS)["duplicates"]
            check("stats expose both fraction and percent",
                  abs(dstats["line_ratio"] - d.ratio) < 1e-9
                  and abs(dstats["line_ratio_pct"] - d.ratio * 100) < 1e-6)
            check("duplicates not cleared when a block remains",
                  dstats["cleared"] is False)

            # Relative paths make Duplo silently report nothing; run_duplo
            # resolves them, so measuring from a relative cwd must still work.
            rel = subprocess.run(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {str(EXP)!r});"
                 "from analysis.tools import run_duplo;"
                 f"print(run_duplo(__import__('pathlib').Path('.'), {str(duplo)!r}, 10, 'cpp').ratio)"],
                cwd=str(root), capture_output=True, text=True)
            check("relative target still measured (paths resolved internally)",
                  rel.stdout.strip().startswith("0.46"), rel.stdout.strip() or rel.stderr[-200:])

    print("\n[7] Lizard reports each function exactly once (regression guard)")
    # Lizard's default tabular output repeats every function that trips its
    # own warning threshold (CCN > 15) in a "!!!! Warnings !!!!" section.
    # Parsing that output double-counted exactly the functions that carry
    # penalty. --csv emits one row per function.
    lizard = Path(sys.executable).parent / "lizard"
    if not lizard.exists():
        print("      SKIP: lizard not found next to the interpreter")
    else:
        from analysis.tools import run_lizard
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wide_body = "\n".join(f"    if (a > {i}) r += {i};" for i in range(1, 21))
            (root / "one.cpp").write_text(
                f"int wide(int a) {{\n    int r = 0;\n{wide_body}\n    return r;\n}}\n")
            (root / "two.cpp").write_text(
                "int small(int a, int b, int c, int d, int e, int f) {\n"
                "    return a;\n}\n\n"
                f"int alsoWide(int a) {{\n    int r = 0;\n{wide_body}\n    return r;\n}}\n")

            recs = run_lizard(root, str(lizard), "cpp")
            names = sorted(r["name"] for r in recs)
            check("one record per function, no warning-section duplicates",
                  len(recs) == 3, f"{len(recs)} records: {names}")
            check("names are each present once",
                  names == ["alsoWide", "small", "wide"], str(names))
            wide_recs = [r for r in recs if r["name"] == "wide"]
            check("the CCN>15 function appears exactly once",
                  len(wide_recs) == 1, f"{len(wide_recs)}")
            check("CCN parsed from the csv column", wide_recs[0]["ccn"] == 21,
                  str(wide_recs[0]["ccn"]))
            check("param parsed from the csv column",
                  next(r for r in recs if r["name"] == "small")["param"] == 6)

            s = compute_metric_stats(recs, [], DuplicationResult(), THRESHOLDS)
            check("penalty population is not inflated",
                  s["ccn"]["functions"] == 3, str(s["ccn"]["functions"]))
            check("exactly two CCN violations counted",
                  s["ccn"]["over_threshold"] == 2, str(s["ccn"]["over_threshold"]))

    print("\n[8] console rendering is Table 4.1 shaped")
    text = format_metric_stats(before)
    for col in ("metric", "thr", "mean", "median", "p90", "p95", "p99", "max"):
        check(f"column '{col}' present", col in text)
    check("duplicates row shows blocks", "blocks=7" in text)

    print("\n[9] test-directory exclusion (§4.3.2)")
    from analysis.tools import (
        DEFAULT_EXCLUDE_DIRS, _exclude_set, _iter_source_files,
        _is_test_source_file,
    )

    check("the two universal names are covered",
          {"test", "tests"} <= set(DEFAULT_EXCLUDE_DIRS), str(DEFAULT_EXCLUDE_DIRS))
    check("None means 'use the default list'",
          _exclude_set(None) == frozenset(DEFAULT_EXCLUDE_DIRS))
    check("an empty list means 'exclude nothing', not 'use the default'",
          _exclude_set([]) == frozenset())
    check("names are normalized to lowercase and stripped",
          _exclude_set(["  Tests ", "FOO"]) == {"tests", "foo"},
          str(sorted(_exclude_set(["  Tests ", "FOO"]))))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Production code plus four differently-named test trees.
        (root / "src").mkdir()
        (root / "src" / "prod.cpp").write_text("int prod() { return 1; }\n")
        for d in ("test", "tests", "Testing", "unit_tests"):
            (root / d).mkdir()
            (root / d / "t.cpp").write_text("int t() { return 1; }\n")
        # A nested test dir under production code must also be pruned.
        (root / "src" / "tests").mkdir()
        (root / "src" / "tests" / "deep.cpp").write_text("int deep() { return 1; }\n")
        # A directory whose name merely contains "test" is NOT a test dir.
        (root / "latest").mkdir()
        (root / "latest" / "keep.cpp").write_text("int keep() { return 1; }\n")

        found = {p.name for p in _iter_source_files(root, "cpp")}
        check("production file kept", "prod.cpp" in found, str(sorted(found)))
        check("test/ and tests/ pruned",
              "t.cpp" not in found, str(sorted(found)))
        check("nested tests/ pruned too", "deep.cpp" not in found)
        check("match is case-insensitive (Testing/)", "t.cpp" not in found)
        check("substring match does not over-exclude (latest/ kept)",
              "keep.cpp" in found, str(sorted(found)))
        check("only the production files remain",
              found == {"prod.cpp", "keep.cpp"}, str(sorted(found)))

        # Counted as paths, not basenames: the four test trees each hold a
        # file called t.cpp, so a set of names would silently collapse them.
        opted_out = list(_iter_source_files(root, "cpp", exclude_dirs=[]))
        check("excluding nothing measures every file",
              len(opted_out) == 7,
              f"{len(opted_out)}: {sorted(p.name for p in opted_out)}")

        custom = {
            p.name for p in _iter_source_files(root, "cpp", exclude_dirs=["latest"])
        }
        check("a custom list replaces the default rather than extending it",
              "keep.cpp" not in custom and "t.cpp" in custom, str(sorted(custom)))

        # Test files often live directly beside production files, especially
        # in Go packages; directory pruning alone cannot exclude them.
        (root / "src" / "engine_test.cpp").write_text(
            "int engine_test() { return 1; }\n"
        )
        (root / "src" / "test_engine.cc").write_text(
            "int test_engine() { return 1; }\n"
        )
        (root / "src" / "contest.cpp").write_text(
            "int contest() { return 1; }\n"
        )
        (root / "src" / "engine_test.go").write_text(
            "package src\nfunc TestEngine() {}\n"
        )
        cpp_names = {
            path.name for path in _iter_source_files(root, "cpp")
        }
        go_names = {
            path.name for path in _iter_source_files(root, "go")
        }
        check("co-located C/C++ test files are excluded",
              "engine_test.cpp" not in cpp_names
              and "test_engine.cc" not in cpp_names)
        check("a production name merely containing test is retained",
              "contest.cpp" in cpp_names)
        check("co-located Go *_test.go files are excluded",
              "engine_test.go" not in go_names)
        check("direct test-file targets are excluded consistently",
              list(_iter_source_files(
                  root / "src" / "engine_test.go", "go",
              )) == [])
        check("test filename classifier is language-specific",
              _is_test_source_file(Path("x_test.go"), "go")
              and not _is_test_source_file(Path("contest.go"), "go"))

    print("\n[10] Duplo -ml reaches the tool and defaults to Duplo's own 4")
    import inspect
    from analysis.tools import run_static_analysis as _rsa
    check("run_duplo defaults to Duplo's -ml default of 4",
          inspect.signature(run_duplo).parameters["min_block_lines"].default == 4)
    check("run_static_analysis carries the same default",
          inspect.signature(_rsa).parameters["duplo_min_block_lines"].default == 4)

    if not duplo.exists():
        print("      SKIP: bin/duplo not present")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # A 6-line duplicated block: found at -ml 4, suppressed at -ml 10.
            block = "\n".join(f"    int v{i} = {i} * 7;" for i in range(1, 7))
            src = f"#include <cstdio>\nvoid a() {{\n{block}\n}}\n"
            (root / "x.cpp").write_text(src)
            (root / "y.cpp").write_text(src.replace("void a()", "void b()"))

            at4 = run_duplo(root, str(duplo), 4, "cpp")
            at10 = run_duplo(root, str(duplo), 10, "cpp")
            print(f"      -ml 4  -> blocks={at4.blocks} dup={at4.duplicate_lines} "
                  f"ratio={at4.ratio:.4f}")
            print(f"      -ml 10 -> blocks={at10.blocks} dup={at10.duplicate_lines} "
                  f"ratio={at10.ratio:.4f}")
            check("-ml 4 finds the 6-line duplicate block", at4.blocks == 1,
                  str(at4.blocks))
            check("-ml 10 suppresses it, proving the flag is honoured",
                  at10.blocks == 0, str(at10.blocks))
            check("suppressing blocks lowers the measured ratio",
                  at4.ratio > at10.ratio, f"{at4.ratio:.4f} vs {at10.ratio:.4f}")

            # Exclusion must reach Duplo as well, not just the per-function
            # tools, or the ratio would be measured over a different file set.
            (root / "tests").mkdir()
            (root / "tests" / "z.cpp").write_text(src.replace("void a()", "void c()"))
            with_tests = run_duplo(root, str(duplo), 4, "cpp", exclude_dirs=[])
            without = run_duplo(root, str(duplo), 4, "cpp")
            check("duplicates in tests/ are excluded from the ratio",
                  without.total_lines < with_tests.total_lines,
                  f"{without.total_lines} vs {with_tests.total_lines}")
            check("excluding tests/ leaves the production duplicate intact",
                  without.blocks == 1, str(without.blocks))

    print("\n[11] duplication parameters survive Config -> gate config -> gate")
    # The gate measures in a subprocess and is configured only through
    # .gate_config.json. If either parameter fails to make that hop the
    # gate silently measures a different population than the coordinator.
    import json
    from queue import Queue
    from config import Config
    from agents.programmer import ProgrammerSession
    from merge_gate.gate import MergeGate

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = root / "wt"
        wt.mkdir()
        cfg = Config(
            repo_root=root / "repo", target_subdir="src", work_root=root / "work",
            run_id="plumbing", duplo_min_block_lines=7,
            dupl_binary="/opt/tools/dupl", dupl_threshold_tokens=123,
            exclude_dirs=("spec", "fixtures"),
        )
        session = ProgrammerSession(
            programmer_id="PROG_1", worktree=wt, cfg=cfg, queue=Queue(),
        )
        session._write_gate_config()
        gate_cfg = session._gate_config_path()
        payload = json.loads(gate_cfg.read_text())

        check("exclude_dirs written as a JSON list",
              payload["exclude_dirs"] == ["spec", "fixtures"],
              str(payload.get("exclude_dirs")))
        check("duplo -ml written", payload["duplo_min_block_lines"] == 7,
              str(payload.get("duplo_min_block_lines")))
        check("Go dupl settings written",
              payload["dupl_binary"] == "/opt/tools/dupl"
              and payload["dupl_threshold_tokens"] == 123)
        check("gate config is outside the target worktree",
              not gate_cfg.is_relative_to(wt), str(gate_cfg))
        task_prompt = session._build_task_prompt([{
            "id": "ISSUE-0001", "file_path": "src/a.cpp", "line": 1,
            "issue_type": "complexity", "message": "CCN=21",
        }])
        check("gate command uses the running Python interpreter",
              str(Path(sys.executable)) in task_prompt, sys.executable)
        check("gate command names the external config and issue id",
              str(gate_cfg) in task_prompt
              and "--issue-id <ISSUE-ID>" in task_prompt)
        check("headless agent explicitly allows Bash and starts clean",
              "--bare" in cfg.agent_cli_extra_args
              and "Bash" in " ".join(cfg.agent_cli_extra_args)
              and "--no-session-persistence" in cfg.agent_cli_extra_args)

        # Rebuild the gate exactly as merge_gate/cli.py does.
        gate = MergeGate(
            worktree=wt, repo_root=cfg.repo_root, target_subdir="src",
            thresholds=payload["thresholds"], build_cmd=payload["build_cmd"],
            test_cmd=payload["test_cmd"],
            integration_branch=payload["integration_branch"],
            duplo_min_block_lines=int(payload.get("duplo_min_block_lines", 4)),
            dupl_binary=payload.get("dupl_binary", ""),
            dupl_threshold_tokens=int(payload.get("dupl_threshold_tokens", 100)),
            exclude_dirs=payload.get("exclude_dirs"),
        )
        check("gate received the custom exclusion list",
              gate.exclude_dirs == ["spec", "fixtures"], str(gate.exclude_dirs))
        check("gate received the custom -ml", gate.duplo_min_block_lines == 7,
              str(gate.duplo_min_block_lines))
        check("gate received the custom dupl settings",
              gate.dupl_binary == "/opt/tools/dupl"
              and gate.dupl_threshold_tokens == 123)
        check("a gate config without the key falls back to the tools default",
              MergeGate(
                  worktree=wt, repo_root=cfg.repo_root, target_subdir="src",
                  thresholds={}, build_cmd=[], test_cmd=[],
                  integration_branch="main",
              ).exclude_dirs is None)

    print("\n[12] Go dupl canonicalizes reverse pairs and overlapping ranges")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        a, b, c = root / "a.go", root / "b.go", root / "c.go"
        counts = {a: 30, b: 30, c: 30}
        plumbing = "\n".join((
            f"{a}:2-10: duplicate of {b}:3-11",
            f"{b}:3-11: duplicate of {a}:2-10",
            f"{a}:8-15: duplicate of {c}:4-11",
            f"{c}:4-11: duplicate of {a}:8-15",
        ))
        parsed_dupl = _parse_go_dupl_plumbing(plumbing, counts)
        check("reverse reports count as two unique clone pairs",
              parsed_dupl.blocks == 2, str(parsed_dupl.blocks))
        check("overlap in a.go is counted once",
              parsed_dupl.duplicate_lines == 14 + 9 + 8,
              str(parsed_dupl.duplicate_lines))
        check("dupl ratio uses bounded physical-line accounting",
              parsed_dupl.ratio == parsed_dupl.duplicate_lines / 90
              and 0 <= parsed_dupl.ratio <= 1)

    dupl = Path(sys.executable).parent / "dupl"
    if not dupl.exists():
        print("      SKIP: dupl not found next to the interpreter")
    else:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = "\n".join(f"\tv{i} := input + {i}" for i in range(20))
            source = f"package sample\n\nfunc alpha(input int) int {{\n{body}\n\treturn v19\n}}\n"
            (root / "a.go").write_text(source)
            (root / "b.go").write_text(source.replace("alpha", "beta"))
            (root / "ignored_test.go").write_text(source.replace("alpha", "testHelper"))
            live = run_go_dupl(root, str(dupl), 20)
            check("installed dupl detects the Go clone", live.blocks >= 1,
                  str(live.blocks))
            check("co-located _test.go is outside dupl's denominator",
                  live.total_lines == 2 * len(source.splitlines()),
                  str(live.total_lines))
            check("real dupl ratio remains bounded", 0 < live.ratio <= 1,
                  str(live.ratio))

    print("\n[13] a misspelled metric key is rejected, not silently ignored")
    # --weights/--thresholds merge into the defaults, so an unknown key is
    # accepted and then never read. Spelling LLOC the way the thesis does
    # would run the complete model instead of the LLOC ablation.
    import main as main_mod

    def parse(*extra):
        argv = [
            "main.py", "--repo", "/tmp/r", "--work-root", "/tmp/w",
            "--build-cmd", "echo build", "--test-cmd", "echo test", *extra,
        ]
        saved, sys.argv = sys.argv, argv
        try:
            return main_mod.parse_args()
        finally:
            sys.argv = saved

    check("the real ablation key works",
          parse("--weights", '{"nloc":0}').weights["nloc"] == 0)
    for label, args_ in (
        ("weights", ("--weights", '{"lloc":0}')),
        ("thresholds", ("--thresholds", '{"lloc":30}')),
    ):
        try:
            parse(*args_)
            check(f"--{label} rejects the thesis's 'lloc' spelling", False,
                  "accepted silently")
        except SystemExit as exc:
            msg = str(exc)
            check(f"--{label} rejects the thesis's 'lloc' spelling", True)
            check(f"--{label} error names the offending key", "lloc" in msg, msg)
            check(f"--{label} error names the valid keys", "nloc" in msg, msg)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
