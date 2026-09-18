"""Verify provider-neutral, resume-safe token accounting."""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from coordination import token_usage as tu                              # noqa: E402
from coordination.model_pricing import get_model_price                  # noqa: E402


FAILURES = []


def check(label, condition, detail=""):
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


print("\n[1] provider usage shapes normalize without double counting")
anthropic = tu.normalize_usage({
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 300,
    "output_tokens": 40,
})
check("Anthropic input includes all billing classes",
      anthropic["input_tokens"] == 420, str(anthropic))
check("Anthropic total adds output once", anthropic["total_tokens"] == 460)

deepseek = tu.normalize_usage({
    "prompt_tokens": 500,
    "prompt_cache_hit_tokens": 350,
    "prompt_cache_miss_tokens": 150,
    "completion_tokens": 60,
})
check("DeepSeek hit/miss replaces, rather than duplicates, prompt total",
      deepseek["input_tokens"] == 500
      and deepseek["cache_read_input_tokens"] == 350
      and deepseek["total_tokens"] == 560, str(deepseek))
check("SDK objects are accepted",
      tu.normalize_usage(SimpleNamespace(
          input_tokens=7, output_tokens=5,
      ))["total_tokens"] == 12)
openrouter_price = get_model_price(
    "openrouter", "anthropic/claude-opus-5",
)
check("OpenRouter Opus slug resolves to the pinned equivalent price",
      openrouter_price is not None
      and openrouter_price.input_usd_per_mtok == 5.0
      and openrouter_price.output_usd_per_mtok == 25.0)


print("\n[2] CLI result aggregate and killed-session fallback")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    complete = root / "ANALYST_1_001.log"
    complete.write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 90, "output_tokens": 10}},
        }),
        json.dumps({
            "type": "result",
            "num_turns": 4,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 50,
                "output_tokens": 20,
            },
        }),
    ]))
    record = tu.parse_agent_log(
        complete, provider="anthropic", model="test",
    )
    check("final result wins over per-turn assistant usage",
          record["input_tokens"] == 150
          and record["output_tokens"] == 20
          and record["turns_or_calls"] == 4)
    check("role, agent and cost are retained",
          record["role"] == "analyst"
          and record["agent"] == "ANALYST_1"
          and record["reported_cost_usd"] == 0.01)

    partial = root / "PROG_2_003.log"
    partial.write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 20, "output_tokens": 3}},
        }),
    ]))
    recovered = tu.parse_agent_log(
        partial, provider="anthropic", model="test",
    )
    check("killed session falls back to assistant messages",
          recovered["usage_source"] == "assistant_messages"
          and recovered["input_tokens"] == 30
          and recovered["output_tokens"] == 5)


print("\n[3] append-only orchestrator accounting survives a truncated tail")
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "orchestrator.jsonl"
    tu.append_orchestrator_usage(
        path,
        usage=SimpleNamespace(input_tokens=50, output_tokens=5),
        provider="anthropic", model="m", call_type="assignment",
    )
    tu.append_orchestrator_usage(
        path,
        usage={"prompt_cache_hit_tokens": 80,
               "prompt_cache_miss_tokens": 20,
               "output_tokens": 10},
        provider="deepseek", model="m", call_type="stuck_evaluation",
    )
    with path.open("a") as fh:
        fh.write('{"truncated":')
    records = tu.load_orchestrator_usage(path)
    check("two completed calls survive", len(records) == 2, str(len(records)))
    check("call types remain visible",
          [r["dispatch"] for r in records]
          == ["assignment", "stuck_evaluation"])


print("\n[4] raw orchestrator responses retain parser input and SDK payload")
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "orchestrator_raw_responses.jsonl"
    response = SimpleNamespace(
        model_dump=lambda mode="python": {
            "id": "msg_test",
            "content": [{"type": "text", "text": "PROG_1: ISSUE-0001"}],
        },
    )
    tu.append_orchestrator_raw_response(
        path,
        response=response,
        parsed_text="PROG_1: ISSUE-0001",
        structured_input={"programmer_assignments": []},
        provider="deepseek",
        model="m",
        call_type="assignment",
    )
    raw_record = json.loads(path.read_text().strip())
    check("raw SDK response is retained",
          raw_record["response"]["content"][0]["text"] == "PROG_1: ISSUE-0001")
    check("exact parser input is retained",
          raw_record["text_forwarded_to_parser"] == "PROG_1: ISSUE-0001")
    check("structured parser input is retained",
          raw_record["structured_input_forwarded_to_parser"]
          == {"programmer_assignments": []})


print("\n[5] aggregate artifact and dispatch-ceiling trigger")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    logs = root / "logs"
    logs.mkdir()
    (logs / "PROG_1_001.log").write_text(json.dumps({
        "type": "result",
        "num_turns": 2,
        "usage": {"input_tokens": 200,
                  "cache_read_input_tokens": 50,
                  "output_tokens": 20},
    }) + "\n")
    orch = root / "orchestrator.jsonl"
    tu.append_orchestrator_usage(
        orch, usage={"input_tokens": 30, "output_tokens": 5},
        provider="anthropic", model="claude-opus-5", call_type="assignment",
    )
    detail = root / "token_usage.json"
    summary = tu.build_token_artifacts(
        agent_log_dir=logs,
        orchestrator_log=orch,
        detail_path=detail,
        provider="anthropic",
        agent_model="claude-opus-5",
        successful_merges=1,
    )
    check("detailed artifact written", detail.exists())
    check("role and per-merge totals are present",
          set(summary["by_role"]) == {"orchestrator", "programmer"}
          and summary["tokens_per_successful_merge"]["total_tokens"] == 305.0,
          str(summary["totals"]))
    check("coverage separates logs and SDK calls",
          summary["coverage"]["agent_logs"] == 1
          and summary["coverage"]["orchestrator_calls"] == 1)
    check("input ceiling triggers at equality",
          tu.budget_trigger(summary["totals"], max_input_tokens=280)
          == "input_tokens")
    check("zero limits disable dispatch ceilings",
          tu.budget_trigger(summary["totals"]) == "")
    check("effective cost is estimated when provider cost is absent",
          summary["totals"]["effective_cost_usd"] > 0
          and summary["totals"]["records_with_estimated_cost"] == 2)
    check("cost ceiling triggers at equality",
          tu.budget_trigger(
              summary["totals"],
              max_cost_usd=summary["totals"]["effective_cost_usd"],
          ) == "cost_usd")
    check("reported/effective cost distinction is explicit",
          "DeepSeek" in summary["notes"]["reported_cost_usd"]
          and "pinned" in summary["notes"]["effective_cost_usd"])


print("\n[6] DeepSeek USD cost ignores the CLI's CNY-valued *_usd field")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "PROG_1_001.log"
    log.write_text(json.dumps({
        "type": "result",
        "num_turns": 2,
        # Historical DeepSeek logs put a CNY value under this misleading key.
        "total_cost_usd": 99.0,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "modelUsage": {
            "deepseek-v4-pro[1m]": {
                "inputTokens": 1_000_000,
                "cacheReadInputTokens": 1_000_000,
                "cacheCreationInputTokens": 0,
                "outputTokens": 1_000_000,
            },
            "deepseek-v4-flash": {
                "inputTokens": 1_000_000,
                "cacheReadInputTokens": 1_000_000,
                "cacheCreationInputTokens": 0,
                "outputTokens": 1_000_000,
            },
        },
    }) + "\n")
    record = tu._decorate_cost(tu.parse_agent_log(
        log, provider="deepseek", model="deepseek-v4-pro[1m]",
    ))
    expected = 0.435 + 0.003625 + 0.87 + 0.14 + 0.0028 + 0.28
    expected_cny = 3.0 + 0.025 + 6.0 + 1.0 + 0.02 + 2.0
    check("Pro and Flash token populations are both counted",
          record["input_tokens"] == 4_000_000
          and record["output_tokens"] == 2_000_000)
    check("DeepSeek ignores the CNY-valued total_cost_usd field",
          abs(record["effective_cost_usd"] - expected) < 1e-8
          and record["cost_source"] == "pinned_price_estimate",
          str(record["effective_cost_usd"]))
    check("DeepSeek native RMB estimate uses official CNY prices",
          abs(record["effective_cost_cny"] - expected_cny) < 1e-8,
          str(record["effective_cost_cny"]))
    check("RMB ceiling triggers without exchange-rate conversion",
          tu.budget_trigger(record, max_cost_cny=expected_cny) == "cost_cny")


print("\n[7] subscription cost is API-equivalent, never incremental billing")
with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "ANALYST_1_001.log"
    log.write_text(json.dumps({
        "type": "result",
        "total_cost_usd": 99.0,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "modelUsage": {
            "claude-opus-5": {
                "inputTokens": 1000,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "outputTokens": 100,
            },
        },
    }) + "\n")
    record = tu._decorate_cost(tu.parse_agent_log(
        log, provider="subscription", model="claude-opus-5",
    ))
    check("subscription ignores raw CLI cost as an actual charge",
          record["effective_cost_usd"] != 99.0
          and record["cost_source"] == "api_equivalent_estimate")


print("\n" + "=" * 62)
print(
    f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES)
    if FAILURES else "ALL CHECKS PASSED"
)
raise SystemExit(1 if FAILURES else 0)
