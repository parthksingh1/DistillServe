"""FastAPI application factory for the DistillServe gateway.

Everything the process needs is wired here and nowhere else: logging, tracing,
metrics, CORS and routers. A factory rather than a module-level ``app`` means
tests can build an isolated instance per settings fixture instead of mutating
global state.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from distillserve_gateway import __version__
from distillserve_gateway.api import chat, health, identity, platform
from distillserve_gateway.bootstrap import (
    build_pipeline,
    build_platform_store,
    build_rollout_controller,
    build_telemetry,
    build_tenants,
)
from distillserve_gateway.core import metrics
from distillserve_gateway.core.build_info import git_sha, git_sha_short
from distillserve_gateway.core.settings import Settings, get_settings
from distillserve_otel import configure_logging, configure_tracing, get_logger, shutdown_tracing

log = get_logger(__name__)


def _route_label(request: Request) -> str:
    """Return the matched route template, or ``"<unmatched>"``.

    Labelling with the template (``/v1/chat/completions``) rather than the raw
    path keeps metric cardinality bounded even when paths carry ids.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "<unmatched>"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured gateway application.

    Args:
        settings: Explicit settings, primarily for tests. Defaults to the
            process-wide singleton parsed from the environment.

    Returns:
        A ready-to-serve FastAPI application.
    """
    resolved = settings or get_settings()

    configure_logging(level=resolved.log_level, json_logs=resolved.log_json)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start and stop process-wide resources around the serving window."""
        configure_tracing(
            service_name=resolved.service_name,
            service_version=__version__,
            environment=resolved.environment.value,
            endpoint=resolved.otlp_traces_endpoint if resolved.langfuse_enabled else None,
            langfuse_public_key=(
                resolved.langfuse_public_key.get_secret_value()
                if resolved.langfuse_public_key
                else None
            ),
            langfuse_secret_key=(
                resolved.langfuse_secret_key.get_secret_value()
                if resolved.langfuse_secret_key
                else None
            ),
            sample_ratio=resolved.otel_sample_ratio,
            extra_resource_attributes={
                "distillserve.mode": resolved.mode.value,
                "distillserve.git_sha": git_sha_short(),
            },
        )
        metrics.set_build_info(
            service=resolved.service_name,
            version=__version__,
            git_sha=git_sha_short(),
            mode=resolved.mode.value,
            environment=resolved.environment.value,
        )
        log.info(
            "gateway.started",
            mode=resolved.mode.value,
            environment=resolved.environment.value,
            git_sha=git_sha_short(),
            langfuse_export=resolved.langfuse_enabled,
        )
        try:
            yield
        finally:
            shutdown_tracing()
            log.info("gateway.stopped")

    app = FastAPI(
        title="DistillServe Gateway",
        version=__version__,
        summary="OpenAI-compatible gateway with routing, caching, guardrails and telemetry.",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def record_request_metrics(
        request: Request, call_next: object
    ) -> Response:  # pragma: no cover - exercised indirectly by every route test
        """Time every request and record it against the gateway's registry."""
        started = time.perf_counter()
        response: Response = await call_next(request)  # type: ignore[operator]
        label = _route_label(request)
        metrics.HTTP_REQUEST_DURATION.labels(method=request.method, path=label).observe(
            time.perf_counter() - started
        )
        metrics.HTTP_REQUESTS.labels(
            method=request.method, path=label, status=str(response.status_code)
        ).inc()
        return response

    # Routes depend on `get_settings`, but the app was built from `resolved`.
    # Overriding the dependency makes the app's own settings authoritative, so a
    # test (or a second app in one process) can never be answered from the
    # process-wide env singleton.
    app.dependency_overrides[get_settings] = lambda: resolved

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(identity.router)
    app.include_router(platform.router)

    # Collaborators are built once, at wiring time, and hung off app.state.
    # Building them per request would re-read the price sheet and re-create the
    # provider client on every call; resolving them from a module global would
    # make two apps in one process share a backend.
    app.state.settings = resolved
    app.state.git_sha = git_sha()
    app.state.pipeline = build_pipeline(resolved)
    app.state.tenants = build_tenants(resolved)
    app.state.telemetry = build_telemetry(resolved)
    app.state.platform_store = build_platform_store(resolved)
    app.state.rollout_controller = build_rollout_controller(resolved)
    return app
