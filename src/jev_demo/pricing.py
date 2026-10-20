"""Cost estimation from per-model token rates (credits per 1M tokens)."""

from __future__ import annotations

from .config import Config
from .metrics import TokenUsage


def credits_for(cfg: Config, surface: str, model: str, usage: TokenUsage) -> tuple[float, bool]:
    """Return (credits, priced). priced=False when the model has no rate in pricing.yaml."""
    rates = cfg.pricing.get(surface, {}).get(model)
    if not rates:
        return 0.0, False
    uncached = max(usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens, 0)
    credits = (
        uncached * rates.get("input", 0)
        + usage.cache_read_tokens * rates.get("cache_read", rates.get("input", 0))
        + usage.cache_write_tokens * rates.get("cache_write", rates.get("input", 0))
        + usage.output_tokens * rates.get("output", 0)
    ) / 1_000_000
    return credits, True


def to_usd(cfg: Config, credits: float) -> float:
    return credits * cfg.credit_price_usd
