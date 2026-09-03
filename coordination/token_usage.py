"""Provider-neutral token accounting for SDK calls and CLI stream-json logs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coordination.model_pricing import get_model_price, pricing_snapshot


_COUNT_FIELDS = (
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "turns_or_calls",
    "sessions",
    "records_with_reported_cost",
    "records_with_estimated_cost",
)
_MONEY_FIELDS = (
    "reported_cost_usd",
    "estimated_cost_usd",
    "effective_cost_usd",
    "estimated_cost_cny",
    "effective_cost_cny",
)
_LOG_NAME = re.compile(
    r"^(?P<agent>(?P<prefix>ANALYST|PROG)_\d+)_(?P<dispatch>\d+)\.log$"
)


def _mapping(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        return data if isinstance(data, dict) else {}
    result = {}
    for name in (
        "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    ):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def empty_usage() -> dict:
    return {
        **{name: 0 for name in _COUNT_FIELDS},
        **{name: 0.0 for name in _MONEY_FIELDS},
    }


def normalize_usage(
    raw_usage: Any,
    *,
    turns_or_calls: int = 1,
    sessions: int = 0,
    reported_cost_usd: Any = None,
) -> dict:
    """Normalize Anthropic, Claude Code and DeepSeek usage shapes.

    Anthropic reports uncached input plus separate cache-creation/read fields.
    DeepSeek may instead report prompt cache hit/miss fields alongside a total
    prompt count. In that shape the prompt total must not be added again.
    """
    raw = _mapping(raw_usage)
    prompt_hit = _nonnegative_int(raw.get("prompt_cache_hit_tokens"))
    prompt_miss = _nonnegative_int(raw.get("prompt_cache_miss_tokens"))

    if prompt_hit or prompt_miss:
        uncached = prompt_miss
        cache_creation = 0
        cache_read = prompt_hit
    else:
        uncached = _nonnegative_int(
            raw.get("input_tokens", raw.get("prompt_tokens", 0))
        )
        cache_creation = _nonnegative_int(
            raw.get("cache_creation_input_tokens")
        )
        cache_read = _nonnegative_int(raw.get("cache_read_input_tokens"))

    cache_creation_detail = _mapping(raw.get("cache_creation"))
    cache_creation_5m = _nonnegative_int(
        cache_creation_detail.get("ephemeral_5m_input_tokens")
    )
    cache_creation_1h = _nonnegative_int(
        cache_creation_detail.get("ephemeral_1h_input_tokens")
    )
    if cache_creation_5m + cache_creation_1h == 0 and cache_creation:
        # Older SDK/CLI records expose only the aggregate. Claude Code's
        # standard prompt cache uses the 5-minute class, so retain that
        # auditable assumption instead of silently dropping cache-write cost.
        cache_creation_5m = cache_creation
    elif cache_creation_5m + cache_creation_1h:
        cache_creation = cache_creation_5m + cache_creation_1h

    output = _nonnegative_int(
        raw.get("output_tokens", raw.get("completion_tokens", 0))
    )
    total_input = uncached + cache_creation + cache_read

    try:
        cost = max(0.0, float(reported_cost_usd))
        has_cost = reported_cost_usd is not None
    except (TypeError, ValueError):
        cost = 0.0
        has_cost = False

    return {
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": cache_creation,
        "cache_creation_5m_input_tokens": cache_creation_5m,
        "cache_creation_1h_input_tokens": cache_creation_1h,
        "cache_read_input_tokens": cache_read,
        "input_tokens": total_input,
        "output_tokens": output,
        "total_tokens": total_input + output,
        "turns_or_calls": _nonnegative_int(turns_or_calls),
        "sessions": _nonnegative_int(sessions),
        "reported_cost_usd": round(cost, 8),
        "estimated_cost_usd": 0.0,
        "effective_cost_usd": 0.0,
        "estimated_cost_cny": 0.0,
        "effective_cost_cny": 0.0,
        "records_with_reported_cost": 1 if has_cost else 0,
        "records_with_estimated_cost": 0,
    }


def add_usage(left: dict, right: dict) -> dict:
    total = empty_usage()
    for name in _COUNT_FIELDS:
        total[name] = _nonnegative_int(left.get(name)) + _nonnegative_int(
            right.get(name)
        )
    for name in _MONEY_FIELDS:
        total[name] = round(
            float(left.get(name, 0.0)) + float(right.get(name, 0.0)), 8,
        )
    return total


def estimate_cost_usd(usage: dict, *, provider: str, model: str) -> float | None:
    """Estimate one usage record from the pinned official price snapshot."""
    price = get_model_price(provider, model)
    if price is None:
        return None
    value = (
        float(usage.get("uncached_input_tokens", 0))
        * price.input_usd_per_mtok
        + float(usage.get("cache_creation_5m_input_tokens", 0))
        * price.cache_write_5m_usd_per_mtok
        + float(usage.get("cache_creation_1h_input_tokens", 0))
        * price.cache_write_1h_usd_per_mtok
        + float(usage.get("cache_read_input_tokens", 0))
        * price.cache_read_usd_per_mtok
        + float(usage.get("output_tokens", 0))
        * price.output_usd_per_mtok
    ) / 1_000_000.0
    return round(value, 8)


def estimate_cost_cny(usage: dict, *, provider: str, model: str) -> float | None:
    """Estimate one usage record from pinned official RMB prices."""
    price = get_model_price(provider, model)
    if price is None or price.input_cny_per_mtok is None:
        return None
    value = (
        float(usage.get("uncached_input_tokens", 0))
        * price.input_cny_per_mtok
        + float(usage.get("cache_creation_5m_input_tokens", 0))
        * float(price.cache_write_5m_cny_per_mtok)
        + float(usage.get("cache_creation_1h_input_tokens", 0))
        * float(price.cache_write_1h_cny_per_mtok)
        + float(usage.get("cache_read_input_tokens", 0))
        * float(price.cache_read_cny_per_mtok)
        + float(usage.get("output_tokens", 0))
        * float(price.output_cny_per_mtok)
    ) / 1_000_000.0
    return round(value, 8)


def _decorate_cost(record: dict) -> dict:
    """Attach estimated and effective run-cost values to one record.

    Claude Code reports Anthropic cost accurately, so that value is preferred
    there.  DeepSeek-backed historical logs expose a ``total_cost_usd`` key
    whose value is denominated in CNY despite the key name; DeepSeek therefore
    always uses the pinned USD estimate, including per-model subagent usage.
    """
    value = dict(record)
    model_usage = value.get("model_usage") or []
    estimates: list[float] = []
    if model_usage:
        for item in model_usage:
            estimate = estimate_cost_usd(
                item,
                provider=str(value.get("provider", "")),
                model=str(item.get("model", "")),
            )
            if estimate is None:
                estimates = []
                break
            estimates.append(estimate)
        estimated = round(sum(estimates), 8) if estimates else None
        cny_estimates = [
            estimate_cost_cny(
                item,
                provider=str(value.get("provider", "")),
                model=str(item.get("model", "")),
            )
            for item in model_usage
        ]
        estimated_cny = (
            round(sum(float(item) for item in cny_estimates), 8)
            if cny_estimates and all(item is not None for item in cny_estimates)
            else None
        )
    else:
        estimated = estimate_cost_usd(
            value,
            provider=str(value.get("provider", "")),
            model=str(value.get("model", "")),
        )
        estimated_cny = estimate_cost_cny(
            value,
            provider=str(value.get("provider", "")),
            model=str(value.get("model", "")),
        )

    has_reported = bool(value.get("records_with_reported_cost"))
    if estimated is not None:
        value["estimated_cost_usd"] = estimated
        value["records_with_estimated_cost"] = 1
    if estimated_cny is not None:
        value["estimated_cost_cny"] = estimated_cny
        value["effective_cost_cny"] = estimated_cny
    provider = str(value.get("provider", ""))
    if provider == "anthropic" and has_reported:
        value["effective_cost_usd"] = round(
            float(value.get("reported_cost_usd", 0.0)), 8,
        )
        value["cost_source"] = "provider_reported"
    elif estimated is not None:
        value["effective_cost_usd"] = estimated
        value["cost_source"] = "pinned_price_estimate"
    elif has_reported:
        value["effective_cost_usd"] = round(
            float(value.get("reported_cost_usd", 0.0)), 8,
        )
        value["cost_source"] = "provider_reported_fallback"
    else:
        value["effective_cost_usd"] = 0.0
        value["cost_source"] = "unavailable"
    return value


def append_orchestrator_usage(
    path: Path,
    *,
    usage: Any,
    provider: str,
    model: str,
    call_type: str,
) -> None:
    """Append one completed synchronous SDK request as one atomic JSONL line."""
    record = {
        "timestamp": round(time.time(), 3),
        "source": "orchestrator_api",
        "role": "orchestrator",
        "agent": "ORCHESTRATOR",
        "dispatch": call_type,
        "provider": provider,
        "model": model,
        "usage_available": bool(_mapping(usage)),
        **normalize_usage(usage, turns_or_calls=1),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def append_orchestrator_raw_response(
    path: Path,
    *,
    response: Any,
    parsed_text: str,
    structured_input: Any = None,
    provider: str,
    model: str,
    call_type: str,
) -> None:
    """Append the complete SDK response used by the orchestrator parser.

    Agent CLI stream-json events already live in each agent log.  The
    orchestrator is synchronous, so without this companion artefact its
    response vanished after parsing and protocol failures could not be
    diagnosed.  SDK response models are serialized only; request headers and
    API credentials are never present in this record.
    """
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            payload = dump(mode="json")
        except TypeError:
            payload = dump()
    else:
        payload = response
    record = {
        "timestamp": round(time.time(), 3),
        "source": "orchestrator_api",
        "role": "orchestrator",
        "agent": "ORCHESTRATOR",
        "dispatch": call_type,
        "provider": provider,
        "model": model,
        "response": payload,
        "text_forwarded_to_parser": parsed_text,
        "structured_input_forwarded_to_parser": structured_input,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def load_orchestrator_usage(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open() as fh:
        for raw in fh:
            # A process crash can leave only the final append incomplete.
            if not raw.endswith("\n"):
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def parse_agent_log(path: Path, *, provider: str, model: str) -> dict:
    """Extract aggregate session usage, preferring Claude Code's result event.

    A final result event is already session-aggregate usage. If a process was
    killed before emitting it, assistant-message usages are summed instead.
    Stream delta events are ignored because they duplicate this accounting.
    """
    match = _LOG_NAME.match(path.name)
    agent = match.group("agent") if match else path.stem
    role = (
        "analyst" if agent.startswith("ANALYST_")
        else "programmer" if agent.startswith("PROG_")
        else "unknown"
    )
    dispatch: str | int = (
        int(match.group("dispatch")) if match else path.name
    )
    assistant_usages = []
    result_usage = None
    result_cost = None
    result_turns = None
    result_model_usage = None

    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "assistant":
            usage = _mapping((obj.get("message") or {}).get("usage"))
            if usage:
                assistant_usages.append(usage)
        elif obj.get("type") == "result":
            candidate = _mapping(obj.get("usage"))
            if candidate:
                result_usage = candidate
                result_cost = obj.get("total_cost_usd")
                result_turns = obj.get("num_turns")
                model_usage = obj.get("modelUsage")
                if isinstance(model_usage, Mapping):
                    result_model_usage = dict(model_usage)

    if result_usage is not None:
        normalized_models = []
        if result_model_usage:
            usage = empty_usage()
            for model_name, raw in result_model_usage.items():
                if not isinstance(raw, Mapping):
                    continue
                normalized = normalize_usage({
                    "input_tokens": raw.get("inputTokens", 0),
                    "cache_creation_input_tokens": raw.get(
                        "cacheCreationInputTokens", 0
                    ),
                    "cache_read_input_tokens": raw.get(
                        "cacheReadInputTokens", 0
                    ),
                    "output_tokens": raw.get("outputTokens", 0),
                })
                normalized_models.append({
                    "model": str(model_name),
                    **normalized,
                })
                usage = add_usage(usage, normalized)
            usage["turns_or_calls"] = _nonnegative_int(result_turns) or 1
            usage["sessions"] = 1
            try:
                usage["reported_cost_usd"] = round(
                    max(0.0, float(result_cost)), 8,
                )
                usage["records_with_reported_cost"] = 1
            except (TypeError, ValueError):
                pass
        else:
            usage = normalize_usage(
                result_usage,
                turns_or_calls=_nonnegative_int(result_turns) or 1,
                sessions=1,
                reported_cost_usd=result_cost,
            )
        available = True
        source = "result"
    else:
        usage = empty_usage()
        for item in assistant_usages:
            usage = add_usage(
                usage, normalize_usage(item, turns_or_calls=1)
            )
        usage["sessions"] = 1
        available = bool(assistant_usages)
        source = "assistant_messages" if available else "unavailable"

    record = {
        "source": "agent_log",
        "usage_source": source,
        "log": path.name,
        "role": role,
        "agent": agent,
        "dispatch": dispatch,
        "provider": provider,
        "model": model,
        "usage_available": available,
        **usage,
    }
    if result_usage is not None and normalized_models:
        record["model_usage"] = normalized_models
    return record


def _aggregate(records: list[dict]) -> dict:
    total = empty_usage()
    for record in records:
        total = add_usage(total, record)
    return total


def _by(records: list[dict], key: str) -> dict:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(str(record.get(key, "unknown")), []).append(record)
    return {name: _aggregate(items) for name, items in sorted(grouped.items())}


def collect_token_usage(
    *,
    agent_log_dir: Path,
    orchestrator_log: Path,
    provider: str,
    agent_model: str,
    successful_merges: int,
) -> dict:
    """Collect a resume-safe snapshot without writing any output file."""
    agent_records = [
        _decorate_cost(parse_agent_log(path, provider=provider, model=agent_model))
        for path in sorted(agent_log_dir.glob("*.log"))
    ] if agent_log_dir.exists() else []
    orchestrator_records = [
        _decorate_cost(record)
        for record in load_orchestrator_usage(orchestrator_log)
    ]
    records = [*orchestrator_records, *agent_records]
    totals = _aggregate(records)
    available = sum(bool(r.get("usage_available")) for r in records)

    per_merge = None
    if successful_merges > 0:
        per_merge = {
            name: round(totals[name] / successful_merges, 2)
            for name in (
                "input_tokens", "output_tokens", "total_tokens",
                "effective_cost_usd",
            )
        }

    return {
        "provider": provider,
        "totals": totals,
        "by_role": _by(records, "role"),
        "by_agent": _by(records, "agent"),
        "tokens_per_successful_merge": per_merge,
        "cost": {
            "effective_cost_usd": totals["effective_cost_usd"],
            "estimated_cost_usd": totals["estimated_cost_usd"],
            "reported_cost_usd": totals["reported_cost_usd"],
            "effective_cost_cny": totals["effective_cost_cny"],
            "estimated_cost_cny": totals["estimated_cost_cny"],
            "pricing": pricing_snapshot(),
        },
        "coverage": {
            "records": len(records),
            "records_with_usage": available,
            "agent_logs": len(agent_records),
            "agent_logs_with_usage": sum(
                bool(r.get("usage_available")) for r in agent_records
            ),
            "orchestrator_calls": len(orchestrator_records),
        },
        "dispatches": records,
        "notes": {
            "input_tokens": (
                "uncached + cache_creation + cache_read; provider cache "
                "breakdowns are normalized without double counting"
            ),
            "turns_or_calls": (
                "SDK requests plus Claude Code num_turns; useful for "
                "comparison but not guaranteed to equal HTTP request count"
            ),
            "reported_cost_usd": (
                "raw CLI field; accurate USD for Anthropic, but historical "
                "DeepSeek logs use a misleading *_usd key for a CNY value"
            ),
            "effective_cost_usd": (
                "Anthropic reported CLI cost plus pinned-price estimates for "
                "SDK calls; DeepSeek always uses pinned official estimates"
            ),
            "effective_cost_cny": (
                "DeepSeek only: pinned official RMB estimate used by the "
                "native CNY dispatch ceiling"
            ),
        },
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".tokens-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_token_artifacts(
    *,
    agent_log_dir: Path,
    orchestrator_log: Path,
    detail_path: Path,
    provider: str,
    agent_model: str,
    successful_merges: int,
) -> dict:
    """Write detailed accounting and return the compact summary block."""
    detail = collect_token_usage(
        agent_log_dir=agent_log_dir,
        orchestrator_log=orchestrator_log,
        provider=provider,
        agent_model=agent_model,
        successful_merges=successful_merges,
    )
    _atomic_json(detail_path, detail)
    return {
        "totals": detail["totals"],
        "by_role": detail["by_role"],
        "by_agent": detail["by_agent"],
        "tokens_per_successful_merge": detail["tokens_per_successful_merge"],
        "cost": detail["cost"],
        "coverage": detail["coverage"],
        "notes": detail["notes"],
        "detail_file": detail_path.name,
        "orchestrator_events_file": orchestrator_log.name,
    }


def budget_trigger(
    totals: dict,
    *,
    max_input_tokens: int = 0,
    max_output_tokens: int = 0,
    max_cost_usd: float = 0.0,
    max_cost_cny: float = 0.0,
) -> str:
    """Return the reached dispatch ceiling, or an empty string when open."""
    if max_input_tokens and totals.get("input_tokens", 0) >= max_input_tokens:
        return "input_tokens"
    if max_output_tokens and totals.get("output_tokens", 0) >= max_output_tokens:
        return "output_tokens"
    if max_cost_usd and totals.get("effective_cost_usd", 0.0) >= max_cost_usd:
        return "cost_usd"
    if max_cost_cny and totals.get("effective_cost_cny", 0.0) >= max_cost_cny:
        return "cost_cny"
    return ""
