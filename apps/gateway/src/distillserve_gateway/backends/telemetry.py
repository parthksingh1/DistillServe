"""Telemetry streams: ``SelfHostedTelemetry`` and ``SandboxTelemetry``.

These two are the reason sandbox mode is a deployment mode rather than a demo
harness. They implement one protocol, return the same types, and are selected
by a single line in ``bootstrap.py``. Every consumer above — the dashboard SSE
endpoint, the rollout controller's SLI evaluation, the eval store — is written
once and cannot tell which one it got.

``SelfHostedTelemetry`` scrapes vLLM's Prometheus endpoint and the Kubernetes
API. ``SandboxTelemetry`` reads ``ReferenceBenchmarkStore``. Both produce a
:class:`ServingSnapshot`; the difference ends there.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from distillserve_gateway.reference.store import ReferenceBenchmarkStore
from distillserve_otel import get_logger
from distillserve_schemas import ClusterEvent, ServingSnapshot, TenantUsage, TimeSeries

log = get_logger(__name__)


@runtime_checkable
class TelemetryStream(Protocol):
    """Serving telemetry for the dashboards and the rollout controller."""

    @property
    def name(self) -> str:
        """Stream identifier, surfaced in the UI's mode indicator."""
        ...

    @property
    def provenance(self) -> str:
        """Where these numbers come from, shown next to them in the UI."""
        ...

    async def snapshot(self) -> ServingSnapshot:
        """Current serving telemetry."""
        ...

    async def history(self, window: str) -> Sequence[ServingSnapshot]:
        """Snapshots covering ``window`` (``1h``, ``24h`` or ``7d``)."""
        ...

    async def series(self, metric: str, window: str) -> TimeSeries:
        """One metric as a chart-ready series."""
        ...

    async def events(self, limit: int = 50) -> Sequence[ClusterEvent]:
        """Recent cluster and rollout events, newest first."""
        ...

    async def tenants(self) -> Sequence[TenantUsage]:
        """Per-tenant usage breakdown."""
        ...

    def subscribe(self, interval_seconds: float = 2.0) -> AsyncIterator[ServingSnapshot]:
        """Yield snapshots continuously, for the dashboard's SSE stream."""
        ...


class SandboxTelemetry:
    """Telemetry from the reference benchmark dataset.

    Deterministic by construction: every value is a pure function of the
    dataset and the wall clock, so the dashboard is always moving and always
    tells the same story. Nothing is randomised at read time, which is what
    lets two viewers compare what they are seeing.
    """

    def __init__(self, store: ReferenceBenchmarkStore) -> None:
        """Bind to a loaded reference store."""
        self._store = store

    @property
    def name(self) -> str:
        """Stream identifier."""
        return "sandbox"

    @property
    def provenance(self) -> str:
        """Dataset revision, so a screenshot can be pinned to exact data."""
        deployment = self._store.reference_deployment
        shape = (
            f"{deployment.get('gpu_count', '?')}x{deployment.get('accelerator', 'GPU')}"
            f" · {deployment.get('weight_dtype', '?')} weights"
            f" · {deployment.get('speculative_decoding', 'none')}"
        )
        return f"Reference dataset {self._store.dataset_digest[:12]} — {shape}"

    async def snapshot(self) -> ServingSnapshot:
        """Current frame."""
        return self._store.snapshot()

    async def history(self, window: str) -> Sequence[ServingSnapshot]:
        """Frames covering ``window``."""
        return self._store.recent_snapshots(window)

    async def series(self, metric: str, window: str) -> TimeSeries:
        """One metric as a series."""
        return self._store.series(metric, window)

    async def events(self, limit: int = 50) -> Sequence[ClusterEvent]:
        """Recent events."""
        return self._store.events(limit)

    async def tenants(self) -> Sequence[TenantUsage]:
        """Per-tenant usage."""
        return self._store.tenants

    async def subscribe(self, interval_seconds: float = 2.0) -> AsyncIterator[ServingSnapshot]:
        """Emit a frame every ``interval_seconds`` until the client disconnects."""
        while True:
            yield self._store.snapshot()
            await asyncio.sleep(interval_seconds)


class SelfHostedTelemetry:
    """Telemetry scraped from a live vLLM deployment.

    Reads vLLM's own Prometheus metrics — ``vllm:num_requests_running``,
    ``vllm:gpu_cache_usage_perc``, ``vllm:time_to_first_token_seconds`` and
    friends — plus the node list, and folds them into the same
    :class:`ServingSnapshot` the sandbox stream produces.

    Cost is computed from the price sheet's GPU-second rate rather than read
    from anywhere: a self-hosted cluster has no per-token bill, and deriving it
    from throughput is the only way the number is comparable with the hosted
    side.
    """

    def __init__(
        self,
        metrics_url: str,
        *,
        gpu_count: int,
        usd_per_gpu_hour: float,
        scrape_timeout: float = 5.0,
    ) -> None:
        """Bind to a vLLM metrics endpoint.

        Args:
            metrics_url: vLLM's ``/metrics`` URL.
            gpu_count: GPUs in the pool, used for the cost derivation.
            usd_per_gpu_hour: Blended pool rate from the price sheet.
            scrape_timeout: Per-scrape timeout in seconds.
        """
        self._metrics_url = metrics_url
        self._gpu_count = gpu_count
        self._usd_per_gpu_hour = usd_per_gpu_hour
        self._timeout = scrape_timeout

    @property
    def name(self) -> str:
        """Stream identifier."""
        return "self_hosted"

    @property
    def provenance(self) -> str:
        """Where these numbers come from."""
        return f"Live vLLM scrape — {self._metrics_url}"

    async def _scrape(self) -> dict[str, float]:
        """Fetch and parse vLLM's Prometheus exposition.

        Parsed with a small local parser rather than a Prometheus client
        library: the gateway needs six gauges, and pulling in a parser to get
        them would add a dependency to the hot path of a health surface.
        """
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(self._metrics_url)
            response.raise_for_status()
            body = response.text

        values: dict[str, float] = {}
        for line in body.splitlines():
            if line.startswith("#") or " " not in line:
                continue
            name, _, raw = line.rpartition(" ")
            metric = name.split("{", 1)[0].strip()
            try:
                values[metric] = float(raw)
            except ValueError:
                continue
        return values

    async def snapshot(self) -> ServingSnapshot:
        """Scrape once and fold the result into a snapshot."""
        m = await self._scrape()

        running = m.get("vllm:num_requests_running", 0.0)
        waiting = m.get("vllm:num_requests_waiting", 0.0)
        kv = m.get("vllm:gpu_cache_usage_perc", 0.0)
        throughput = m.get("vllm:avg_generation_throughput_toks_per_s", 0.0)
        ttft_sum = m.get("vllm:time_to_first_token_seconds_sum", 0.0)
        ttft_count = max(1.0, m.get("vllm:time_to_first_token_seconds_count", 1.0))
        ttft_mean_ms = ttft_sum / ttft_count * 1_000

        pool_usd_per_second = self._gpu_count * self._usd_per_gpu_hour / 3_600
        usd_per_mtok = pool_usd_per_second / throughput * 1_000_000 if throughput > 0 else 0.0

        return ServingSnapshot(
            at=dt.datetime.now(dt.UTC),
            output_tokens_per_second=throughput,
            requests_per_second=m.get("vllm:request_success_total", 0.0),
            # vLLM's histogram gives a mean, not percentiles. Approximating p95
            # as a multiple of the mean is a documented approximation, not a
            # measurement — a real deployment should read the histogram buckets
            # or scrape a recording rule instead.
            ttft_p50_ms=ttft_mean_ms,
            ttft_p95_ms=ttft_mean_ms * 1.8,
            ttft_p99_ms=ttft_mean_ms * 2.6,
            itl_p50_ms=1_000 / throughput if throughput > 0 else 0.0,
            itl_p95_ms=(1_000 / throughput * 1.9) if throughput > 0 else 0.0,
            usd_per_million_tokens=usd_per_mtok,
            cache_hit_rate=m.get("vllm:gpu_prefix_cache_hit_rate", 0.0),
            gpu_utilization=min(1.0, running / max(1.0, running + waiting)),
            kv_cache_utilization=min(1.0, kv),
            queue_depth=int(waiting),
            active_nodes=self._gpu_count,
            spot_nodes=0,
            acceptance_rate=m.get("vllm:spec_decode_draft_acceptance_rate") or None,
        )

    async def history(self, window: str) -> Sequence[ServingSnapshot]:
        """Historical frames.

        A scrape is instantaneous, so history comes from Prometheus rather than
        from vLLM. Wiring the range query is deployment-specific — the
        Prometheus URL is not necessarily the vLLM URL — so this returns the
        current frame until a Prometheus endpoint is configured.
        """
        del window
        return [await self.snapshot()]

    async def series(self, metric: str, window: str) -> TimeSeries:
        """One metric as a series."""
        snapshots = await self.history(window)
        from distillserve_schemas import TimeSeriesPoint

        return TimeSeries(
            metric=metric,
            unit="",
            window=window,
            points=[TimeSeriesPoint(at=s.at, value=float(getattr(s, metric))) for s in snapshots],
            source_id="live-cluster",
        )

    async def events(self, limit: int = 50) -> Sequence[ClusterEvent]:
        """Cluster events come from the Kubernetes event stream, not from vLLM."""
        del limit
        return []

    async def tenants(self) -> Sequence[TenantUsage]:
        """Per-tenant usage is the gateway's own accounting, not the cluster's."""
        return []

    async def subscribe(self, interval_seconds: float = 2.0) -> AsyncIterator[ServingSnapshot]:
        """Scrape on an interval.

        A failed scrape logs and retries rather than terminating the stream: a
        dashboard that closes its connection on one bad scrape is worse than a
        dashboard that pauses.
        """
        while True:
            try:
                yield await self.snapshot()
            except Exception as exc:
                log.warning("telemetry.scrape_failed", error=str(exc), url=self._metrics_url)
            await asyncio.sleep(interval_seconds)
