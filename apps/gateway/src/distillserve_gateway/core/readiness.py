"""Dependency probes behind ``GET /readyz``.

A probe is a small async callable returning a :class:`ReadinessCheck`. Keeping
them in a list rather than hard-coding an if-ladder in the route means later
phases add a dependency (Redis, SQLite registry, vLLM endpoint) by appending
one function — the route never changes.

Probes are ``required`` or not. Required failures make the service unready so
the load balancer stops sending traffic; optional failures only degrade it,
because a gateway with a cold semantic cache is still a useful gateway.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from distillserve_gateway.core.settings import Settings
from distillserve_schemas import DeploymentMode, HealthStatus, ReadinessCheck

#: A probe takes the resolved settings and reports one check result.
ReadinessProbe = Callable[[Settings], Awaitable[ReadinessCheck]]


def _timed(start: float) -> float:
    """Return elapsed milliseconds since ``start`` (a ``perf_counter`` value)."""
    return (time.perf_counter() - start) * 1000.0


async def check_provider_credentials(settings: Settings) -> ReadinessCheck:
    """Verify a hosted provider key exists whenever generation goes through one.

    Required in `hosted` and `sandbox` (sandbox generates via the hosted
    backend); not applicable in `self_hosted`, where vLLM serves generation.
    """
    start = time.perf_counter()
    applicable = settings.mode is not DeploymentMode.SELF_HOSTED
    if not applicable:
        return ReadinessCheck(
            name="provider_credentials",
            status=HealthStatus.OK,
            required=False,
            detail="not applicable in self_hosted mode",
            latency_ms=_timed(start),
        )
    ok = settings.has_any_provider_key
    return ReadinessCheck(
        name="provider_credentials",
        status=HealthStatus.OK if ok else HealthStatus.FAILED,
        required=True,
        detail=None if ok else "no GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY configured",
        latency_ms=_timed(start),
    )


async def check_vllm_endpoint(settings: Settings) -> ReadinessCheck:
    """Verify a vLLM endpoint is configured when self-hosted serving is selected.

    Phase 1 checks configuration only. The live ``/v1/models`` reachability
    probe lands with ``SelfHostedInferenceBackend`` in phase 3, at which point
    this probe calls the backend instead of reading a setting.
    """
    start = time.perf_counter()
    if settings.mode is not DeploymentMode.SELF_HOSTED:
        return ReadinessCheck(
            name="vllm_endpoint",
            status=HealthStatus.OK,
            required=False,
            detail="not applicable outside self_hosted mode",
            latency_ms=_timed(start),
        )
    configured = bool(settings.vllm_endpoint)
    return ReadinessCheck(
        name="vllm_endpoint",
        status=HealthStatus.OK if configured else HealthStatus.FAILED,
        required=True,
        detail=settings.vllm_endpoint if configured else "VLLM_ENDPOINT is not set",
        latency_ms=_timed(start),
    )


async def check_reference_dataset(settings: Settings) -> ReadinessCheck:
    """Verify the reference benchmark dataset is present for sandbox telemetry.

    Required only in `sandbox`, where ``ReferenceBenchmarkStore`` is the
    telemetry source and an absent dataset means empty dashboards.
    """
    start = time.perf_counter()
    required = settings.mode is DeploymentMode.SANDBOX
    path = Path(settings.reference_data_dir)
    # Off the event loop: a stat against a cold or network-backed mount can
    # block for tens of milliseconds, and readiness is polled continuously.
    exists = await asyncio.to_thread(path.is_dir)
    if not required:
        return ReadinessCheck(
            name="reference_dataset",
            status=HealthStatus.OK,
            required=False,
            detail="not applicable outside sandbox mode",
            latency_ms=_timed(start),
        )
    resolved = await asyncio.to_thread(lambda: str(path.resolve())) if exists else None
    return ReadinessCheck(
        name="reference_dataset",
        status=HealthStatus.OK if exists else HealthStatus.FAILED,
        required=True,
        detail=resolved if exists else f"{path} not found",
        latency_ms=_timed(start),
    )


async def check_langfuse_export(settings: Settings) -> ReadinessCheck:
    """Report whether trace export is configured.

    Optional by design: losing observability degrades operability but must not
    take the serving path out of rotation.
    """
    start = time.perf_counter()
    enabled = settings.langfuse_enabled
    return ReadinessCheck(
        name="langfuse_export",
        status=HealthStatus.OK if enabled else HealthStatus.DEGRADED,
        required=False,
        detail=settings.otlp_traces_endpoint if enabled else "LANGFUSE_* keys not configured",
        latency_ms=_timed(start),
    )


DEFAULT_PROBES: tuple[ReadinessProbe, ...] = (
    check_provider_credentials,
    check_vllm_endpoint,
    check_reference_dataset,
    check_langfuse_export,
)


def aggregate(checks: list[ReadinessCheck]) -> HealthStatus:
    """Fold individual check results into one service-level status."""
    if any(c.required and c.status is not HealthStatus.OK for c in checks):
        return HealthStatus.FAILED
    if any(c.status is not HealthStatus.OK for c in checks):
        return HealthStatus.DEGRADED
    return HealthStatus.OK
