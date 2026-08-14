"""The OpenAI-compatible chat endpoint.

Two responsibilities only: translate HTTP to and from the pipeline, and turn a
:class:`BackendError` into a structured error the client can act on. All of the
actual behaviour — routing, cost, tracing — lives in
:class:`~distillserve_gateway.pipeline.CompletionPipeline`, so this module stays
short enough to read in one sitting.

The SSE framing follows OpenAI's exactly (``data: {json}`` per frame, a final
``data: [DONE]``), because that is what every OpenAI client already parses.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace

from distillserve_gateway.api.deps import RequireInfer
from distillserve_gateway.backends.base import BackendError
from distillserve_gateway.pipeline import (
    CompletionPipeline,
    InjectionBlockedError,
    RateLimitExceededError,
)
from distillserve_otel import get_logger
from distillserve_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GatewayError,
    GatewayErrorBody,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["inference"])

#: Terminator every OpenAI-compatible client watches for.
_SSE_DONE = "data: [DONE]\n\n"


def get_pipeline(request: Request) -> CompletionPipeline:
    """Return the pipeline bound to this app.

    Resolved from ``app.state`` rather than a module global so that a test — or
    a second app in the same process — gets its own backend and router.
    """
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:  # pragma: no cover - a misconfigured app fails at startup
        raise RuntimeError("No completion pipeline is bound to this application.")
    return pipeline  # type: ignore[no-any-return]


PipelineDep = Annotated[CompletionPipeline, Depends(get_pipeline)]


def _current_trace_id() -> str | None:
    """Return the active trace id, so an error can be correlated in Langfuse."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def _guardrail_response(exc: RateLimitExceededError | InjectionBlockedError) -> JSONResponse:
    """Render a guardrail refusal.

    A tenant rate limit is 429 *with* a Retry-After, because unlike an upstream
    limit this one is ours and we know exactly when the bucket refills. An
    injection block is 400: the request itself is the problem, and retrying it
    unchanged will fail again.
    """
    if isinstance(exc, RateLimitExceededError):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=GatewayError(
                error=GatewayErrorBody(
                    message=str(exc),
                    type="rate_limit_error",
                    code="tenant_rate_limit",
                    trace_id=exc.trace_id,
                    fallback_reason="tenant_rate_limited",
                )
            ).model_dump(mode="json"),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=GatewayError(
            error=GatewayErrorBody(
                message=str(exc),
                type="prompt_injection_error",
                code=exc.verdict.detector,
                trace_id=exc.trace_id,
                fallback_reason="injection_blocked",
            )
        ).model_dump(mode="json"),
    )


def _error_response(exc: BackendError) -> JSONResponse:
    """Render a :class:`BackendError` as an OpenAI-shaped error body.

    A rate limit is reported as 429 with ``fallback_reason`` set: the trace
    already records that the gateway tried and failed to degrade gracefully,
    and the client needs to distinguish "retry shortly" from "this request is
    malformed".
    """
    if exc.rate_limited:
        http_status = status.HTTP_429_TOO_MANY_REQUESTS
        error_type = "rate_limit_error"
        fallback = "upstream_rate_limited"
    elif exc.retryable:
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        error_type = "service_unavailable_error"
        fallback = "upstream_unavailable"
    else:
        http_status = exc.status_code or status.HTTP_502_BAD_GATEWAY
        error_type = "upstream_error"
        fallback = None

    body = GatewayError(
        error=GatewayErrorBody(
            message=str(exc),
            type=error_type,
            code=str(exc.status_code) if exc.status_code else None,
            trace_id=exc.trace_id or _current_trace_id(),
            fallback_reason=fallback,
        )
    )
    return JSONResponse(status_code=http_status, content=body.model_dump(mode="json"))


async def _sse(
    pipeline: CompletionPipeline, body: ChatCompletionRequest, requests_per_minute: int
) -> AsyncIterator[str]:
    """Yield SSE frames for a streaming completion.

    A mid-stream failure cannot change the HTTP status — the 200 and the
    headers are already on the wire — so the error is delivered as a final SSE
    frame followed by ``[DONE]``. Clients that stop at the terminator still
    terminate cleanly; clients that inspect frames see what went wrong.
    """
    try:
        async for chunk in pipeline.stream(body, requests_per_minute=requests_per_minute):
            payload = chunk.model_dump(mode="json", exclude_none=True)
            yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    except (RateLimitExceededError, InjectionBlockedError) as exc:
        # Guardrails fire before the first token, so in practice this lands on
        # frame zero — but it is handled here too because the headers are
        # already committed once streaming has begun.
        error = GatewayError(
            error=GatewayErrorBody(
                message=str(exc),
                type=(
                    "rate_limit_error"
                    if isinstance(exc, RateLimitExceededError)
                    else "prompt_injection_error"
                ),
                trace_id=exc.trace_id or _current_trace_id(),
                fallback_reason=(
                    "tenant_rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "injection_blocked"
                ),
            )
        )
        yield f"data: {json.dumps(error.model_dump(mode='json'), separators=(',', ':'))}\n\n"
    except BackendError as exc:
        error = GatewayError(
            error=GatewayErrorBody(
                message=str(exc),
                type="rate_limit_error" if exc.rate_limited else "upstream_error",
                trace_id=exc.trace_id or _current_trace_id(),
                fallback_reason="upstream_rate_limited" if exc.rate_limited else None,
            )
        )
        yield f"data: {json.dumps(error.model_dump(mode='json'), separators=(',', ':'))}\n\n"

    yield _SSE_DONE


@router.post(
    "/chat/completions",
    summary="Create a chat completion",
    description=(
        "OpenAI-compatible. Point any OpenAI SDK's base_url at this gateway. "
        "Responses carry an extra `distillserve` object with the route decision, "
        "cache outcome, cost and latency breakdown."
    ),
    response_model=None,
    responses={
        400: {"model": GatewayError, "description": "Prompt rejected by the injection screen."},
        401: {"model": GatewayError, "description": "Missing or unrecognised bearer token."},
        403: {"model": GatewayError, "description": "Tenant lacks the `infer` scope."},
        429: {"model": GatewayError, "description": "Upstream provider rate-limited the request."},
        503: {"model": GatewayError, "description": "Upstream provider is unavailable."},
    },
)
async def create_chat_completion(
    body: ChatCompletionRequest, pipeline: PipelineDep, tenant: RequireInfer
) -> ChatCompletionResponse | StreamingResponse | JSONResponse:
    """Serve a chat completion, streaming or buffered."""
    # The authenticated tenant always wins over a caller-supplied one: `tenant`
    # keys rate limits, cost attribution and audit entries, so letting a client
    # name someone else's tenant would let it spend their budget.
    body = body.model_copy(update={"tenant": tenant.id})
    if body.stream:
        return StreamingResponse(
            _sse(pipeline, body, tenant.requests_per_minute),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # Tells nginx and friends not to buffer, which would otherwise
                # collect the whole stream and destroy TTFT.
                "X-Accel-Buffering": "no",
            },
        )

    try:
        return await pipeline.complete(body, requests_per_minute=tenant.requests_per_minute)
    except (RateLimitExceededError, InjectionBlockedError) as exc:
        return _guardrail_response(exc)
    except BackendError as exc:
        return _error_response(exc)
