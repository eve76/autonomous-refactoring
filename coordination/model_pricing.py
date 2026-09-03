"""Pinned model-pricing snapshots used for reproducible cost accounting.

Prices are per million tokens in the provider's published currencies. They are intentionally stored in the
experiment code instead of fetched at run time: provider prices can change,
while an archived run must remain reproducible.  Update the snapshot date and
the table together when intentionally adopting new prices.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


PRICING_SNAPSHOT_DATE = "2026-08-13"
PRICING_SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
}


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_5m_usd_per_mtok: float
    cache_write_1h_usd_per_mtok: float
    input_cny_per_mtok: float | None = None
    cache_read_cny_per_mtok: float | None = None
    output_cny_per_mtok: float | None = None
    cache_write_5m_cny_per_mtok: float | None = None
    cache_write_1h_cny_per_mtok: float | None = None


# Anthropic standard/global API prices.  DeepSeek performs automatic context
# caching, so cache writes are billed as cache-miss input rather than as a
# separate class; both cache-write fields therefore use the miss price.
_PRICES = {
    ("anthropic", "claude-opus-4-7"): ModelPrice(
        provider="anthropic",
        model="claude-opus-4-7",
        input_usd_per_mtok=5.0,
        cache_read_usd_per_mtok=0.5,
        output_usd_per_mtok=25.0,
        cache_write_5m_usd_per_mtok=6.25,
        cache_write_1h_usd_per_mtok=10.0,
    ),
    ("anthropic", "claude-opus-4-6"): ModelPrice(
        provider="anthropic",
        model="claude-opus-4-6",
        input_usd_per_mtok=5.0,
        cache_read_usd_per_mtok=0.5,
        output_usd_per_mtok=25.0,
        cache_write_5m_usd_per_mtok=6.25,
        cache_write_1h_usd_per_mtok=10.0,
    ),
    ("anthropic", "claude-opus-4-5"): ModelPrice(
        provider="anthropic",
        model="claude-opus-4-5",
        input_usd_per_mtok=5.0,
        cache_read_usd_per_mtok=0.5,
        output_usd_per_mtok=25.0,
        cache_write_5m_usd_per_mtok=6.25,
        cache_write_1h_usd_per_mtok=10.0,
    ),
    ("anthropic", "claude-sonnet-4-6"): ModelPrice(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_usd_per_mtok=3.0,
        cache_read_usd_per_mtok=0.3,
        output_usd_per_mtok=15.0,
        cache_write_5m_usd_per_mtok=3.75,
        cache_write_1h_usd_per_mtok=6.0,
    ),
    ("anthropic", "claude-sonnet-4-5"): ModelPrice(
        provider="anthropic",
        model="claude-sonnet-4-5",
        input_usd_per_mtok=3.0,
        cache_read_usd_per_mtok=0.3,
        output_usd_per_mtok=15.0,
        cache_write_5m_usd_per_mtok=3.75,
        cache_write_1h_usd_per_mtok=6.0,
    ),
    ("anthropic", "claude-haiku-4-5"): ModelPrice(
        provider="anthropic",
        model="claude-haiku-4-5",
        input_usd_per_mtok=1.0,
        cache_read_usd_per_mtok=0.1,
        output_usd_per_mtok=5.0,
        cache_write_5m_usd_per_mtok=1.25,
        cache_write_1h_usd_per_mtok=2.0,
    ),
    ("deepseek", "deepseek-v4-pro"): ModelPrice(
        provider="deepseek",
        model="deepseek-v4-pro",
        input_usd_per_mtok=0.435,
        cache_read_usd_per_mtok=0.003625,
        output_usd_per_mtok=0.87,
        cache_write_5m_usd_per_mtok=0.435,
        cache_write_1h_usd_per_mtok=0.435,
        input_cny_per_mtok=3.0,
        cache_read_cny_per_mtok=0.025,
        output_cny_per_mtok=6.0,
        cache_write_5m_cny_per_mtok=3.0,
        cache_write_1h_cny_per_mtok=3.0,
    ),
    ("deepseek", "deepseek-v4-flash"): ModelPrice(
        provider="deepseek",
        model="deepseek-v4-flash",
        input_usd_per_mtok=0.14,
        cache_read_usd_per_mtok=0.0028,
        output_usd_per_mtok=0.28,
        cache_write_5m_usd_per_mtok=0.14,
        cache_write_1h_usd_per_mtok=0.14,
        input_cny_per_mtok=1.0,
        cache_read_cny_per_mtok=0.02,
        output_cny_per_mtok=2.0,
        cache_write_5m_cny_per_mtok=1.0,
        cache_write_1h_cny_per_mtok=1.0,
    ),
}


def canonical_model(provider: str, model: str) -> str:
    """Normalize provider aliases while retaining explicit model versions."""
    if provider == "subscription":
        provider = "anthropic"
    value = str(model or "").strip().lower()
    if provider == "deepseek" and value.endswith("[1m]"):
        value = value[:-4]
    exact = (provider, value)
    if exact in _PRICES:
        return value
    # Anthropic API model identifiers may carry a release-date suffix.
    for known_provider, known_model in _PRICES:
        if known_provider == provider and value.startswith(f"{known_model}-"):
            return known_model
    return value


def get_model_price(provider: str, model: str) -> ModelPrice | None:
    provider = str(provider).strip().lower()
    pricing_provider = "anthropic" if provider == "subscription" else provider
    key = (pricing_provider, canonical_model(pricing_provider, model))
    return _PRICES.get(key)


def require_model_price(provider: str, model: str) -> ModelPrice:
    price = get_model_price(provider, model)
    if price is None:
        raise ValueError(
            f"no pinned pricing for {provider} model {model!r}; "
            "disable the run cost ceiling or add an audited price snapshot"
        )
    return price


def pricing_snapshot() -> dict:
    return {
        "date": PRICING_SNAPSHOT_DATE,
        "currencies": ["USD", "CNY"],
        "unit": "per_million_tokens",
        "sources": dict(PRICING_SOURCES),
        "models": {
            f"{provider}:{model}": asdict(price)
            for (provider, model), price in sorted(_PRICES.items())
        },
    }
