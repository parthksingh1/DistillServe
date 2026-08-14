"""Composition root: settings in, wired collaborators out.

This is the *only* module allowed to branch on ``DISTILLSERVE_MODE``. Every
other module receives an ``InferenceBackend`` and behaves identically whichever
one it got. Concentrating the branch here is what makes the three modes drop-in
equivalents rather than three code paths that happen to look similar.
"""

from __future__ import annotations

from distillserve_gateway.backends.base import InferenceBackend
from distillserve_gateway.backends.hosted import HostedInferenceBackend
from distillserve_gateway.backends.sandbox import SandboxInferenceBackend
from distillserve_gateway.backends.self_hosted import SelfHostedInferenceBackend
from distillserve_gateway.backends.telemetry import (
    SandboxTelemetry,
    SelfHostedTelemetry,
    TelemetryStream,
)
from distillserve_gateway.core.auth import TenantDirectory, demo_tenant, load_directory
from distillserve_gateway.core.cache import (
    Embedder,
    HostedEmbedder,
    LexicalEmbedder,
    SemanticCache,
)
from distillserve_gateway.core.pricing import PriceSheet, get_price_sheet
from distillserve_gateway.core.ratelimit import InMemoryRateLimiter, RateLimiter
from distillserve_gateway.core.settings import Settings
from distillserve_gateway.guardrails.injection import InjectionScreen
from distillserve_gateway.guardrails.pii import PIIRedactor
from distillserve_gateway.pipeline import CompletionPipeline
from distillserve_gateway.platform.registry import PlatformStore
from distillserve_gateway.platform.rollouts import AuditLog, RolloutController
from distillserve_gateway.reference.store import ReferenceBenchmarkStore
from distillserve_gateway.routing.router import Router, RouterConfig
from distillserve_otel import get_logger
from distillserve_schemas import DeploymentMode

log = get_logger(__name__)


def build_provider_keys(settings: Settings) -> dict[str, str]:
    """Collect configured provider credentials, keyed by LiteLLM provider name."""
    candidates = {
        "groq": settings.groq_api_key,
        "gemini": settings.gemini_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }
    return {
        provider: secret.get_secret_value()
        for provider, secret in candidates.items()
        if secret is not None
    }


def build_tenants(settings: Settings) -> TenantDirectory:
    """Assemble the tenant directory from the config file plus the demo tenant.

    The demo tenant is appended rather than written into the file so that
    enabling demo access is one environment variable on a deployment, not a
    config commit — and so rotating the token is a redeploy, not an edit.
    """
    directory = load_directory(settings.tenants_file)
    tenants = list(directory.tenants)

    # An empty token disables demo access. Treating it as a real credential
    # would create a tenant that authenticates on an empty bearer header.
    token = settings.demo_token.get_secret_value() if settings.demo_token else ""
    if token:
        tenants = [t for t in tenants if t.id != "demo"]
        tenants.append(demo_tenant(token, requests_per_minute=settings.demo_requests_per_minute))
        log.info("bootstrap.demo_tenant_enabled", rpm=settings.demo_requests_per_minute)

    return TenantDirectory(version=directory.version, tenants=tenants)


def build_reference_store(settings: Settings) -> ReferenceBenchmarkStore:
    """Load the reference benchmark dataset."""
    return ReferenceBenchmarkStore(settings.reference_data_dir, seed=settings.reference_seed)


def build_backend(settings: Settings) -> InferenceBackend:
    """Select and construct the inference backend for the active mode.

    This is the branch. Everything above it receives an ``InferenceBackend``
    and behaves identically whichever of the three it got — which is what makes
    the modes drop-in equivalents rather than three parallel code paths.
    """
    hosted = HostedInferenceBackend(
        api_keys=build_provider_keys(settings),
        teacher_model=settings.teacher_model,
        student_model=settings.student_model,
    )

    if settings.mode is DeploymentMode.SELF_HOSTED:
        if settings.vllm_endpoint is None:  # pragma: no cover - settings validate this
            raise ValueError("self_hosted mode requires VLLM_ENDPOINT")
        return SelfHostedInferenceBackend(settings.vllm_endpoint)

    if settings.mode is DeploymentMode.SANDBOX:
        return SandboxInferenceBackend(hosted, SandboxTelemetry(build_reference_store(settings)))

    return hosted


def build_telemetry(settings: Settings) -> TelemetryStream:
    """Select the telemetry stream for the active mode.

    `hosted` mode has no GPU pool of its own, so it reads the reference stream
    too — the dashboards would otherwise be empty, and the cluster shape they
    describe is documented as reference data either way.
    """
    if settings.mode is DeploymentMode.SELF_HOSTED and settings.vllm_endpoint:
        sheet = get_price_sheet()
        return SelfHostedTelemetry(
            f"{settings.vllm_endpoint.rstrip('/').removesuffix('/v1')}/metrics",
            gpu_count=sheet.self_hosted.gpu_count,
            usd_per_gpu_hour=sheet.self_hosted.blended_usd_per_gpu_hour,
        )
    return SandboxTelemetry(build_reference_store(settings))


def build_platform_store(settings: Settings) -> PlatformStore:
    """Open the platform store and seed it from the reference dataset once."""
    store = PlatformStore(settings.platform_db_path)
    if store.is_empty():
        reference = build_reference_store(settings)
        store.seed(
            model_versions=reference.model_versions,
            adapters=reference.adapters,
            eval_reports=reference.eval_reports,
        )
    return store


def build_rollout_controller(settings: Settings) -> RolloutController:
    """Open the audit log and load the current rollout set."""
    audit = AuditLog(
        settings.audit_db_path,
        secret=settings.audit_secret.get_secret_value() if settings.audit_secret else None,
    )
    return RolloutController(audit, build_reference_store(settings).rollouts)


def build_router(settings: Settings) -> Router:
    """Construct the router from the configured teacher and student."""
    return Router(
        RouterConfig(
            teacher_model=settings.teacher_model,
            student_model=settings.student_model,
        )
    )


#: Embedding models paired with each provider, used by the semantic cache
#: when DISTILLSERVE_EMBEDDING_MODEL is not set explicitly.
PROVIDER_EMBEDDING_MODELS: dict[str, str] = {
    "gemini": "gemini/text-embedding-004",
    "openai": "openai/text-embedding-3-small",
}


def build_embedder(settings: Settings) -> Embedder:
    """Choose an embedder for the semantic cache.

    Preference order is: an explicitly configured model, then the configured
    provider's embedding endpoint, then a dependency-free lexical embedder.

    The lexical fallback is a real fallback, not a stub — it genuinely catches
    retries and near-duplicates — but it reports ``semantic = False`` so the
    dashboard says "lexical mode" rather than implying paraphrase recall the
    deployment does not have.
    """
    keys = build_provider_keys(settings)

    model = settings.embedding_model
    if model is None:
        for provider in settings.configured_providers:
            if provider in PROVIDER_EMBEDDING_MODELS:
                model = PROVIDER_EMBEDDING_MODELS[provider]
                break

    if model is None:
        log.info("bootstrap.cache_lexical_mode", reason="no embedding model available")
        return LexicalEmbedder()

    provider = model.split("/", 1)[0]
    return HostedEmbedder(model, api_key=keys.get(provider))


def build_cache(settings: Settings) -> SemanticCache:
    """Construct the semantic cache from settings."""
    return SemanticCache(
        build_embedder(settings),
        similarity_threshold=settings.cache_similarity_threshold,
        ttl_seconds=settings.cache_ttl_seconds,
        enabled=settings.cache_enabled,
    )


def build_injection_screen(settings: Settings) -> InjectionScreen | None:
    """Construct the prompt-injection screen, or None when disabled."""
    if not settings.injection_defense_enabled:
        return None
    return InjectionScreen(
        block_threshold=settings.injection_block_threshold,
        flag_threshold=settings.injection_flag_threshold,
    )


def build_redactor(settings: Settings) -> PIIRedactor | None:
    """Construct the PII redactor, or None when disabled."""
    return PIIRedactor() if settings.pii_redaction_enabled else None


def build_rate_limiter(settings: Settings) -> RateLimiter | None:
    """Construct the rate limiter, or None when disabled.

    Redis is not wired here yet: an in-memory bucket that honestly reports
    ``distributed = False`` is better than a Redis client that silently fails
    open on a connection error. The Redis limiter ships with the cache backend
    in the next slice.
    """
    if not settings.rate_limit_enabled:
        return None
    if settings.redis_url is None:
        log.info("bootstrap.rate_limit_in_memory", reason="REDIS_URL not configured")
    return InMemoryRateLimiter()


def build_pipeline(
    settings: Settings, *, price_sheet: PriceSheet | None = None
) -> CompletionPipeline:
    """Assemble the completion pipeline for this process."""
    return CompletionPipeline(
        backend=build_backend(settings),
        router=build_router(settings),
        price_sheet=price_sheet or get_price_sheet(),
        mode=settings.mode,
        cache=build_cache(settings),
        injection=build_injection_screen(settings),
        redactor=build_redactor(settings),
        rate_limiter=build_rate_limiter(settings),
        default_requests_per_minute=settings.default_requests_per_minute,
    )
