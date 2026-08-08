"""Prometheus metrics for the gateway.

Metric objects are module-level singletons registered against a dedicated
``CollectorRegistry``. Using our own registry rather than the global default
keeps a test that imports the app twice from raising ``Duplicated timeseries``,
and it keeps third-party libraries' default-registry metrics out of our
scrape unless we opt them in.

Label cardinality is the thing that kills a Prometheus server, so labels here
are strictly bounded: mode, route and outcome are all small closed enums.
Anything unbounded (tenant id, prompt hash) belongs on a span, not a metric.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

BUILD_INFO = Gauge(
    "distillserve_build_info",
    "Always 1; labels carry the build identity of the running process.",
    labelnames=("service", "version", "git_sha", "mode", "environment"),
    registry=REGISTRY,
)

HTTP_REQUESTS = Counter(
    "distillserve_http_requests_total",
    "HTTP requests handled by the gateway.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "distillserve_http_request_duration_seconds",
    "Wall-clock duration of HTTP requests handled by the gateway.",
    labelnames=("method", "path"),
    # Buckets skew long: an LLM call is seconds, not milliseconds, and the
    # default buckets would put every generation in the +Inf bucket.
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)


COMPLETIONS = Counter(
    "distillserve_completions_total",
    "Chat completions handled, by route and outcome.",
    labelnames=("route", "backend", "outcome"),
    registry=REGISTRY,
)

COMPLETION_TOKENS = Counter(
    "distillserve_completion_tokens_total",
    "Tokens consumed and produced by chat completions.",
    labelnames=("route", "backend", "direction"),
    registry=REGISTRY,
)

COMPLETION_COST_USD = Counter(
    "distillserve_completion_cost_usd_total",
    "Cumulative USD cost of chat completions, priced from config/prices.yaml.",
    labelnames=("route", "backend"),
    registry=REGISTRY,
)

TTFT_SECONDS = Histogram(
    "distillserve_ttft_seconds",
    "Time to first token, by route.",
    labelnames=("route", "backend"),
    # Sub-second resolution matters here in a way it does not for total
    # duration: the SLO the rollout controller gates on is a p95 TTFT in the
    # low hundreds of milliseconds, so the buckets must be dense there.
    buckets=(0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

GUARDRAIL_EVENTS = Counter(
    "distillserve_guardrail_events_total",
    "Guardrail decisions, by guardrail and action.",
    labelnames=("guardrail", "action"),
    registry=REGISTRY,
)

CACHE_LOOKUPS = Counter(
    "distillserve_semantic_cache_lookups_total",
    "Semantic cache lookups, by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)


def set_build_info(
    *, service: str, version: str, git_sha: str, mode: str, environment: str
) -> None:
    """Publish the build identity as a labelled gauge set to 1."""
    BUILD_INFO.labels(
        service=service, version=version, git_sha=git_sha, mode=mode, environment=environment
    ).set(1)
