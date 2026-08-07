"""Cost accounting across hosted providers and self-hosted GPU serving.

Two billing models, one number. Hosted providers bill per token; a GPU pool
bills per second regardless of whether it is decoding. Reducing both to a
``CostBreakdown`` with the same ``total_usd`` is what lets the Playground show
a teacher-versus-student delta, and the eval reports plot a latency/cost
Pareto, without either surface knowing which backend served the request.

The self-hosted derivation is the interesting half:

    usd_per_second   = gpu_count * blended_usd_per_gpu_hour / 3600
    effective_tok_s  = throughput_tok_s * target_utilization
    usd_per_mtok     = usd_per_second / effective_tok_s * 1e6

Utilisation belongs in the denominator because idle GPU-seconds are billed but
produce no tokens. Leaving it out is the standard way a self-hosted cost model
comes out optimistic by 25-40%.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from distillserve_schemas import CostBreakdown, TokenUsage

_TOKENS_PER_MILLION: Final = 1_000_000
_SECONDS_PER_HOUR: Final = 3_600


class ModelPrice(BaseModel):
    """Per-token price for one hosted model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_usd_per_mtok: float = Field(ge=0.0)
    output_usd_per_mtok: float = Field(ge=0.0)
    source: str = Field(description="Where this price was published; checked when it changes.")


class SelfHostedPrice(BaseModel):
    """Rate card for the self-hosted GPU pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accelerator: str
    gpu_count: int = Field(gt=0)
    on_demand_usd_per_gpu_hour: float = Field(gt=0.0)
    spot_usd_per_gpu_hour: float = Field(gt=0.0)
    spot_fraction: float = Field(ge=0.0, le=1.0)
    target_utilization: float = Field(gt=0.0, le=1.0)
    source: str

    @property
    def blended_usd_per_gpu_hour(self) -> float:
        """Spot/on-demand weighted hourly rate for a single GPU."""
        return (
            self.spot_fraction * self.spot_usd_per_gpu_hour
            + (1.0 - self.spot_fraction) * self.on_demand_usd_per_gpu_hour
        )

    @property
    def pool_usd_per_second(self) -> float:
        """Cost of running the whole pool for one wall-clock second."""
        return self.gpu_count * self.blended_usd_per_gpu_hour / _SECONDS_PER_HOUR

    def usd_per_mtok(self, throughput_tokens_per_second: float) -> float:
        """Derive a per-million-token price from sustained pool throughput.

        Args:
            throughput_tokens_per_second: Output tokens per second the pool
                sustains at full utilisation.

        Returns:
            USD per million output tokens, with idle GPU-seconds included.

        Raises:
            ValueError: if throughput is not positive — a zero-throughput pool
                has undefined per-token cost, and returning ``inf`` would poison
                every downstream average.
        """
        if throughput_tokens_per_second <= 0.0:
            raise ValueError("throughput_tokens_per_second must be positive to derive a price.")
        effective = throughput_tokens_per_second * self.target_utilization
        return self.pool_usd_per_second / effective * _TOKENS_PER_MILLION


class PriceSheet(BaseModel):
    """The full contents of ``config/prices.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    hosted: dict[str, ModelPrice]
    hosted_default: ModelPrice
    self_hosted: SelfHostedPrice

    def hosted_price(self, model: str) -> ModelPrice:
        """Return the price for ``model``, falling back to the default entry.

        The fallback is intentionally expensive rather than free: an unpriced
        model should be visible on the cost dashboard, not invisible.
        """
        return self.hosted.get(model, self.hosted_default)

    def hosted_cost(self, model: str, usage: TokenUsage) -> CostBreakdown:
        """Price a hosted completion from its token counts."""
        price = self.hosted_price(model)
        input_usd = usage.input_tokens / _TOKENS_PER_MILLION * price.input_usd_per_mtok
        output_usd = usage.output_tokens / _TOKENS_PER_MILLION * price.output_usd_per_mtok
        return CostBreakdown(
            total_usd=input_usd + output_usd,
            input_usd=input_usd,
            output_usd=output_usd,
            basis="per_token",
            price_sheet_version=self.version,
        )

    def self_hosted_cost(
        self, usage: TokenUsage, *, throughput_tokens_per_second: float
    ) -> CostBreakdown:
        """Price a self-hosted completion from GPU-seconds.

        Prefill is not free, but it is cheap relative to decode and vLLM's
        continuous batching overlaps it with other requests' decode. Attributing
        the whole pool rate to output tokens keeps the model simple and errs
        toward *over*-stating self-hosted cost, which is the safe direction for
        a claim that self-hosting is cheaper.
        """
        usd_per_mtok = self.self_hosted.usd_per_mtok(throughput_tokens_per_second)
        output_usd = usage.output_tokens / _TOKENS_PER_MILLION * usd_per_mtok
        return CostBreakdown(
            total_usd=output_usd,
            input_usd=0.0,
            output_usd=output_usd,
            basis="per_gpu_second",
            price_sheet_version=self.version,
        )


def _default_path() -> Path:
    """Locate ``config/prices.yaml`` relative to the repository root."""
    # gateway/src/distillserve_gateway/core/pricing.py -> repo root is 5 up.
    return Path(__file__).resolve().parents[5] / "config" / "prices.yaml"


def load_price_sheet(path: Path | str | None = None) -> PriceSheet:
    """Parse a price sheet from disk.

    Args:
        path: Explicit path. Defaults to ``config/prices.yaml`` at the repo root.

    Returns:
        A validated :class:`PriceSheet`.
    """
    resolved = Path(path) if path is not None else _default_path()
    raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return PriceSheet.model_validate(raw)


@lru_cache(maxsize=4)
def get_price_sheet(path: str | None = None) -> PriceSheet:
    """Return a cached price sheet.

    Prices change on a deploy, not on a request, so parsing once per process is
    correct. Tests clear the cache with ``get_price_sheet.cache_clear()``.
    """
    return load_price_sheet(path)
