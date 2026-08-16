"""Tests for the price sheet and cost accounting."""

from __future__ import annotations

import pytest

from distillserve_gateway.core.pricing import PriceSheet, get_price_sheet, load_price_sheet
from distillserve_schemas import TokenUsage

TEACHER = "groq/llama-3.3-70b-versatile"
STUDENT = "groq/llama-3.1-8b-instant"


@pytest.fixture(scope="module")
def sheet() -> PriceSheet:
    return load_price_sheet()


def test_the_checked_in_price_sheet_parses(sheet: PriceSheet) -> None:
    assert sheet.version
    assert TEACHER in sheet.hosted
    assert STUDENT in sheet.hosted


def test_every_hosted_price_cites_a_source(sheet: PriceSheet) -> None:
    """An uncited price is unauditable, and prices drift."""
    for model, price in sheet.hosted.items():
        assert price.source, f"{model} has no source"


def test_hosted_cost_is_priced_per_million_tokens(sheet: PriceSheet) -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = sheet.hosted_cost(TEACHER, usage)
    price = sheet.hosted[TEACHER]

    assert cost.input_usd == pytest.approx(price.input_usd_per_mtok)
    assert cost.output_usd == pytest.approx(price.output_usd_per_mtok)
    assert cost.total_usd == pytest.approx(cost.input_usd + cost.output_usd)
    assert cost.basis == "per_token"
    assert cost.price_sheet_version == sheet.version


def test_the_student_is_materially_cheaper_than_the_teacher(sheet: PriceSheet) -> None:
    """The platform's whole premise; worth pinning as a test."""
    usage = TokenUsage(input_tokens=2_000, output_tokens=800)
    teacher = sheet.hosted_cost(TEACHER, usage).total_usd
    student = sheet.hosted_cost(STUDENT, usage).total_usd

    assert student < teacher / 5


def test_unpriced_models_fall_back_to_an_expensive_default(sheet: PriceSheet) -> None:
    """An unpriced model must stand out on the cost dashboard, not read as free."""
    usage = TokenUsage(input_tokens=1_000, output_tokens=1_000)
    unknown = sheet.hosted_cost("someprovider/unknown-model", usage)

    assert unknown.total_usd > sheet.hosted_cost(TEACHER, usage).total_usd


def test_zero_usage_costs_nothing(sheet: PriceSheet) -> None:
    cost = sheet.hosted_cost(TEACHER, TokenUsage(input_tokens=0, output_tokens=0))
    assert cost.total_usd == 0.0


def test_blended_gpu_rate_sits_between_spot_and_on_demand(sheet: PriceSheet) -> None:
    gpu = sheet.self_hosted
    assert gpu.spot_usd_per_gpu_hour < gpu.blended_usd_per_gpu_hour
    assert gpu.blended_usd_per_gpu_hour < gpu.on_demand_usd_per_gpu_hour


def test_self_hosted_price_falls_as_throughput_rises(sheet: PriceSheet) -> None:
    """Raising tok/s is the mechanism by which self-hosting gets cheaper."""
    slow = sheet.self_hosted.usd_per_mtok(1_000)
    fast = sheet.self_hosted.usd_per_mtok(3_100)

    assert fast < slow
    assert fast == pytest.approx(slow * 1_000 / 3_100)


def test_utilisation_is_in_the_denominator(sheet: PriceSheet) -> None:
    """Idle GPU-seconds are billed; omitting them understates cost."""
    gpu = sheet.self_hosted
    naive = gpu.pool_usd_per_second / 3_100 * 1_000_000
    actual = gpu.usd_per_mtok(3_100)

    assert actual > naive
    assert actual == pytest.approx(naive / gpu.target_utilization)


def test_zero_throughput_is_rejected(sheet: PriceSheet) -> None:
    """Returning inf would poison every downstream average."""
    with pytest.raises(ValueError, match="must be positive"):
        sheet.self_hosted.usd_per_mtok(0.0)


def test_self_hosted_cost_attributes_the_pool_rate_to_output_tokens(sheet: PriceSheet) -> None:
    usage = TokenUsage(input_tokens=10_000, output_tokens=1_000)
    cost = sheet.self_hosted_cost(usage, throughput_tokens_per_second=3_100)

    assert cost.basis == "per_gpu_second"
    assert cost.input_usd == 0.0
    assert cost.total_usd == cost.output_usd
    assert cost.total_usd > 0.0


def test_price_sheet_is_cached() -> None:
    get_price_sheet.cache_clear()
    assert get_price_sheet() is get_price_sheet()
