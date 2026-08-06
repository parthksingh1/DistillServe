"""OpenTelemetry tracer setup and the GenAI attribute vocabulary.

DistillServe exports OTLP/HTTP straight to Langfuse rather than using a vendor
SDK. That choice keeps the instrumentation vendor-neutral — the same spans can
be fanned out to a collector, Tempo or Honeycomb by changing one endpoint — and
it means the attribute names on our spans are the OTel GenAI semantic
conventions rather than a proprietary schema.

Attributes we emit that are *not* in the spec are namespaced under
``distillserve.*`` so they can never collide with a future spec addition.
"""

from __future__ import annotations

import base64
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.util._once import Once

_provider: TracerProvider | None = None


def _reset_global_provider() -> None:
    """Clear OpenTelemetry's install-once global so a provider can be replaced.

    ``trace.set_tracer_provider`` is deliberately one-shot: a library must not
    be able to hijack the application's provider. That is right in production,
    where :func:`configure_tracing` runs exactly once, but it makes the
    function untestable and breaks a lifespan that restarts in-process. Rather
    than leave a warning-and-no-op hiding a misconfiguration, we reset the
    guard explicitly here — the one place in the codebase allowed to touch it.
    """
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()


class GenAIAttributes:
    """Span attribute keys used across the platform.

    Grouped in one place so a rename is a single edit and so the dashboard,
    the eval harness and the gateway cannot disagree about a key's spelling.
    """

    # --- OTel GenAI semantic conventions -----------------------------------
    SYSTEM: Final = "gen_ai.system"
    OPERATION_NAME: Final = "gen_ai.operation.name"
    REQUEST_MODEL: Final = "gen_ai.request.model"
    REQUEST_MAX_TOKENS: Final = "gen_ai.request.max_tokens"
    REQUEST_TEMPERATURE: Final = "gen_ai.request.temperature"
    RESPONSE_MODEL: Final = "gen_ai.response.model"
    RESPONSE_ID: Final = "gen_ai.response.id"
    RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
    USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"

    # --- DistillServe extensions -------------------------------------------
    ROUTE: Final = "distillserve.route"
    CACHE_HIT: Final = "distillserve.cache_hit"
    COST_USD: Final = "distillserve.cost_usd"
    MODE: Final = "distillserve.mode"
    TENANT: Final = "distillserve.tenant"
    ADAPTER: Final = "distillserve.adapter"
    BACKEND: Final = "distillserve.backend"
    TTFT_MS: Final = "distillserve.ttft_ms"
    FALLBACK_REASON: Final = "distillserve.fallback_reason"


def _langfuse_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """Build the OTLP auth header Langfuse expects (HTTP Basic over the key pair)."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def configure_tracing(
    *,
    service_name: str,
    service_version: str,
    environment: str,
    endpoint: str | None = None,
    langfuse_public_key: str | None = None,
    langfuse_secret_key: str | None = None,
    sample_ratio: float = 1.0,
    extra_resource_attributes: dict[str, str] | None = None,
) -> trace.Tracer:
    """Install a global tracer provider and return a tracer for the service.

    When no exporter can be configured (no endpoint, or missing Langfuse keys)
    the provider is still installed with no span processor. That keeps every
    call site unconditional — instrumentation code never has to ask whether
    tracing is on — while exporting nothing, which is the correct behaviour for
    a local run without credentials.

    Args:
        service_name: ``service.name`` resource attribute.
        service_version: ``service.version`` resource attribute.
        environment: ``deployment.environment`` resource attribute.
        endpoint: OTLP/HTTP traces endpoint. Ignored when keys are absent.
        langfuse_public_key: Langfuse public key, used for OTLP basic auth.
        langfuse_secret_key: Langfuse secret key, used for OTLP basic auth.
        sample_ratio: Head sampling ratio in ``[0, 1]``; 1.0 keeps everything.
        extra_resource_attributes: Additional resource attributes to merge.

    Returns:
        A tracer bound to ``service_name``.
    """
    global _provider

    if _provider is not None:
        # Reconfiguring is legitimate (tests, an in-process restart); leaking
        # the old provider's batch processor would silently drop spans.
        shutdown_tracing()

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": environment,
            **(extra_resource_attributes or {}),
        }
    )
    sampler = ParentBased(ALWAYS_ON if sample_ratio >= 1.0 else TraceIdRatioBased(sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)

    if endpoint and langfuse_public_key and langfuse_secret_key:
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=_langfuse_headers(langfuse_public_key, langfuse_secret_key),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    _reset_global_provider()
    trace.set_tracer_provider(provider)
    _provider = provider
    return trace.get_tracer(service_name, service_version)


def shutdown_tracing(timeout_millis: int = 5_000) -> None:
    """Flush and shut down the installed provider, if any.

    Called from the app's lifespan teardown so a scale-down or a redeploy does
    not silently drop the last batch of spans.
    """
    global _provider
    if _provider is not None:
        _provider.force_flush(timeout_millis)
        _provider.shutdown()
        _provider = None
        _reset_global_provider()
