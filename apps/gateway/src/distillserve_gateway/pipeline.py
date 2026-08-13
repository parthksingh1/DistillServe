"""The request pipeline: one path from an HTTP request to tokens on the wire.

Every completion, streaming or not, runs through :class:`CompletionPipeline`.
Keeping a single path is what makes the Traces waterfall honest — the stages it
draws are literally the stages executed here, in this order:

    prompt-injection check -> PII redaction -> semantic cache lookup
      -> router decision -> LLM call -> cost accounting

This slice lands routing, the LLM call, cost accounting and tracing. The
guardrail and cache stages are introduced next; they are already the shape the
pipeline expects, so adding them will not move the LLM call or change what a
client sees.

The span is opened once, around the whole pipeline, and closed when the last
token has been written. A span that closed when the response *started*
streaming would report a TTFT-shaped duration and make every latency dashboard
wrong.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from distillserve_gateway.backends.base import (
    BackendError,
    GenerationRequest,
    InferenceBackend,
)
from distillserve_gateway.core.cache import CachedResponse, SemanticCache
from distillserve_gateway.core.metrics import (
    CACHE_LOOKUPS,
    COMPLETION_COST_USD,
    COMPLETION_TOKENS,
    COMPLETIONS,
    GUARDRAIL_EVENTS,
    TTFT_SECONDS,
)
from distillserve_gateway.core.pricing import PriceSheet
from distillserve_gateway.core.ratelimit import RateLimiter
from distillserve_gateway.guardrails.injection import InjectionScreen, InjectionVerdict
from distillserve_gateway.guardrails.pii import PIIRedactor, RedactionResult
from distillserve_gateway.routing.router import Router
from distillserve_otel import GenAIAttributes, get_logger
from distillserve_schemas import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatRole,
    CostBreakdown,
    DeploymentMode,
    DistillServeMetadata,
    GenerationMetrics,
    RouteDecision,
    TokenUsage,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Everything known about a completion once it has finished."""

    usage: TokenUsage
    cost: CostBreakdown
    route: RouteDecision
    metrics: GenerationMetrics
    finish_reason: str
    response_id: str
    trace_id: str
    cache_hit: bool
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class _Event:
    """One step of the pipeline's internal generator.

    Both public methods consume this same generator, which is what stops the
    streaming and non-streaming paths from drifting apart in routing, cost or
    tracing. Exactly one ``result`` event is emitted, last.
    """

    text: str = ""
    result: PipelineResult | None = None


class RateLimitExceededError(RuntimeError):
    """The tenant's token bucket is empty.

    Distinct from an upstream 429: this one is *our* limit, it is deterministic,
    and the client is told exactly how long to wait.
    """

    def __init__(self, retry_after_seconds: int, limit: int, tenant_id: str) -> None:
        """Record what the caller needs to retry successfully."""
        super().__init__(
            f"Tenant '{tenant_id}' exceeded its limit of {limit} requests/minute. "
            f"Retry in {retry_after_seconds}s."
        )
        self.retry_after_seconds = retry_after_seconds
        self.limit = limit
        self.tenant_id = tenant_id
        self.trace_id: str | None = None


class InjectionBlockedError(RuntimeError):
    """The prompt was refused by the injection screen before any provider call.

    Refusing before the provider call is the point: a blocked injection costs
    nothing and cannot reach the model.
    """

    def __init__(self, verdict: InjectionVerdict) -> None:
        """Record the verdict so the error body and the trace agree."""
        super().__init__(f"Prompt rejected by the injection screen: {verdict.reason}")
        self.verdict = verdict
        self.trace_id: str | None = None


class PipelineError(RuntimeError):
    """The pipeline finished without producing a terminal result.

    Only reachable if a backend's generator is broken, but raising beats
    returning a half-built response: a completion with no usage would silently
    report zero cost.
    """


class CompletionPipeline:
    """Runs a chat completion end to end."""

    def __init__(
        self,
        *,
        backend: InferenceBackend,
        router: Router,
        price_sheet: PriceSheet,
        mode: DeploymentMode,
        cache: SemanticCache | None = None,
        injection: InjectionScreen | None = None,
        redactor: PIIRedactor | None = None,
        rate_limiter: RateLimiter | None = None,
        default_requests_per_minute: int = 60,
        tracer: trace.Tracer | None = None,
    ) -> None:
        """Bind the pipeline to its collaborators.

        Every guardrail is optional and defaults to off rather than to a
        permissive stub, so a deployment that has not configured one cannot be
        under the impression that it is protected.

        Args:
            backend: Where generation is served from.
            router: Chooses teacher, student or adapter.
            price_sheet: Cost model for both billing bases.
            mode: Deployment mode, recorded on every span and response.
            cache: Semantic cache. None disables the lookup entirely.
            injection: Prompt-injection screen.
            redactor: PII redactor.
            rate_limiter: Per-tenant token bucket.
            default_requests_per_minute: Limit applied when a caller does
                not supply the tenant's own.
            tracer: Tracer to use. Defaults to the globally configured one.
        """
        self._backend = backend
        self._router = router
        self._prices = price_sheet
        self._mode = mode
        self._cache = cache
        self._injection = injection
        self._redactor = redactor
        self._rate_limiter = rate_limiter
        self._default_rpm = default_requests_per_minute
        self._tracer = tracer or trace.get_tracer(__name__)

    # -- public API ---------------------------------------------------------

    async def complete(
        self, request: ChatCompletionRequest, *, requests_per_minute: int | None = None
    ) -> ChatCompletionResponse:
        """Run a non-streaming completion."""
        pieces: list[str] = []
        result: PipelineResult | None = None

        async for event in self._run(request, requests_per_minute=requests_per_minute):
            if event.result is not None:
                result = event.result
            elif event.text:
                pieces.append(event.text)

        if result is None:
            raise PipelineError("pipeline produced no terminal result")

        return ChatCompletionResponse(
            id=result.response_id,
            created=int(time.time()),
            model=result.route.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role=ChatRole.ASSISTANT, content="".join(pieces)),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=result.usage,
            distillserve=self._metadata(result),
        )

    async def stream(
        self, request: ChatCompletionRequest, *, requests_per_minute: int | None = None
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Run a streaming completion, yielding OpenAI-shaped chunks.

        The terminal chunk carries usage and the ``distillserve`` metadata;
        content chunks carry only text. A client that ignores the extra key
        sees a stream indistinguishable from OpenAI's.
        """
        created = int(time.time())
        # The response id is ours and is fixed before the first byte, so every
        # frame in the stream shares one id even when the provider assigns its
        # own later (recorded on the span as gen_ai.response.id).
        response_id = _new_response_id()
        first = True

        async for event in self._run(
            request, response_id=response_id, requests_per_minute=requests_per_minute
        ):
            if event.result is not None:
                yield ChatCompletionChunk(
                    id=response_id,
                    created=created,
                    model=event.result.route.model,
                    choices=[
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChatCompletionDelta(),
                            finish_reason=event.result.finish_reason,
                        )
                    ],
                    usage=event.result.usage,
                    distillserve=self._metadata(event.result),
                )
                continue

            yield ChatCompletionChunk(
                id=response_id,
                created=created,
                model=request.model or "",
                choices=[
                    ChatCompletionChunkChoice(
                        index=0,
                        delta=ChatCompletionDelta(
                            role=ChatRole.ASSISTANT if first else None, content=event.text
                        ),
                    )
                ],
            )
            first = False

    # -- internals ----------------------------------------------------------

    async def _run(
        self,
        request: ChatCompletionRequest,
        *,
        response_id: str | None = None,
        requests_per_minute: int | None = None,
    ) -> AsyncIterator[_Event]:
        """Execute the pipeline, emitting text events then one result event.

        Stage order is load-bearing and matches the Traces waterfall exactly:

        1. **Rate limit** first, so an over-quota tenant costs us nothing — not
           an embedding, not a classifier pass, not a provider call.
        2. **Injection screen** before redaction, because redaction rewrites the
           text and would hide the very tokens the screen looks for.
        3. **PII redaction** before the cache, so nothing personal is ever
           written into a cache entry.
        4. **Cache lookup** before generation, because a hit makes it moot.
        5. **Route**, then **generate**, then **cost**.
        """
        prompt = self._latest_user_turn(request)
        tenant_id = request.tenant or "local"
        completion_id = response_id or _new_response_id()

        with self._tracer.start_as_current_span("chat.completions") as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            started = time.perf_counter()

            await self._enforce_rate_limit(
                span, tenant_id, trace_id, requests_per_minute or self._default_rpm
            )
            self._screen_prompt(span, prompt, trace_id)
            redaction = self._redact(span, prompt)

            route = self._router.route(
                prompt=redaction.text,
                policy=request.route_policy,
                explicit_model=request.model,
                declared_task=request.task,
            )
            self._annotate_request(span, request, route)

            cached = await self._lookup_cache(span, redaction.text, tenant_id, route.model)
            if cached is not None:
                async for event in self._serve_from_cache(
                    span, cached, redaction, route, completion_id, trace_id, started
                ):
                    yield event
                return

            ttft: float | None = None
            usage = TokenUsage(input_tokens=0, output_tokens=0)
            finish_reason = "stop"
            provider_id: str | None = None
            pieces: list[str] = []

            try:
                backend_request = self._to_backend_request(request, route, redaction)
                async for chunk in self._backend.generate(backend_request):
                    provider_id = provider_id or chunk.response_id
                    if chunk.content:
                        if ttft is None:
                            ttft = time.perf_counter() - started
                        # Rehydrated per chunk, so a streaming client sees real
                        # values as they arrive rather than placeholders that
                        # only resolve once the stream has ended.
                        text = (
                            redaction.rehydrate(chunk.content)
                            if redaction.redacted
                            else chunk.content
                        )
                        pieces.append(text)
                        yield _Event(text=text)
                    if chunk.finish_reason is not None:
                        finish_reason = chunk.finish_reason
                    if chunk.usage is not None:
                        usage = chunk.usage
            except BackendError as exc:
                exc.trace_id = trace_id
                degraded = await self._degrade(
                    span, exc, redaction, route, tenant_id, completion_id, trace_id, started
                )
                if degraded is not None:
                    async for event in degraded:
                        yield event
                    return
                self._annotate_failure(span, exc, route)
                COMPLETIONS.labels(
                    route=route.target.value,
                    backend=self._backend.name,
                    outcome="rate_limited" if exc.rate_limited else "error",
                ).inc()
                raise

            result = self._finalise(
                route=route,
                usage=usage,
                finish_reason=finish_reason,
                response_id=completion_id,
                trace_id=trace_id,
                ttft=ttft,
                total=time.perf_counter() - started,
            )
            self._annotate_response(span, result, provider_id)
            self._record_metrics(result)
            await self._store_in_cache(redaction.text, "".join(pieces), result, tenant_id)
            yield _Event(result=result)

    # -- stages -------------------------------------------------------------

    async def _enforce_rate_limit(
        self, span: Span, tenant_id: str, trace_id: str, requests_per_minute: int
    ) -> None:
        """Consume a token from the tenant bucket, or refuse the request.

        Raises:
            RateLimitExceededError: when the bucket is empty.
        """
        if self._rate_limiter is None:
            return

        decision = await self._rate_limiter.check(
            tenant_id, requests_per_minute=requests_per_minute
        )
        span.set_attribute("distillserve.ratelimit.remaining", decision.remaining)
        span.set_attribute("distillserve.ratelimit.distributed", decision.distributed)
        if not decision.allowed:
            GUARDRAIL_EVENTS.labels(guardrail="rate_limit", action="block").inc()
            span.set_attribute(GenAIAttributes.FALLBACK_REASON, "tenant_rate_limited")
            error = RateLimitExceededError(decision.retry_after_seconds, decision.limit, tenant_id)
            error.trace_id = trace_id
            raise error

    def _screen_prompt(self, span: Span, prompt: str, trace_id: str) -> None:
        """Score the prompt for injection and act on the verdict.

        Raises:
            InjectionBlockedError: when the verdict is BLOCK.
        """
        if self._injection is None:
            return

        verdict = self._injection.screen(prompt)
        span.set_attribute("distillserve.injection.score", verdict.score)
        span.set_attribute("distillserve.injection.action", verdict.action.value)
        span.set_attribute("distillserve.injection.detector", verdict.detector)
        if verdict.matched:
            span.set_attribute("distillserve.injection.signals", list(verdict.matched))
        GUARDRAIL_EVENTS.labels(guardrail="injection", action=verdict.action.value).inc()

        if verdict.blocked:
            log.warning(
                "pipeline.injection_blocked",
                score=verdict.score,
                signals=list(verdict.matched),
            )
            error = InjectionBlockedError(verdict)
            error.trace_id = trace_id
            raise error

    def _redact(self, span: Span, prompt: str) -> RedactionResult:
        """Redact PII from the prompt, recording what was found.

        The count and the *types* go on the span; the values never do. A trace
        is exactly as sensitive as what you choose to put in it.
        """
        if self._redactor is None:
            return RedactionResult(text=prompt)

        result = self._redactor.redact(prompt)
        span.set_attribute("distillserve.pii.redacted", result.redacted)
        span.set_attribute("distillserve.pii.count", len(result.matches))
        if result.matches:
            span.set_attribute(
                "distillserve.pii.types", sorted({m.type.value for m in result.matches})
            )
            GUARDRAIL_EVENTS.labels(guardrail="pii", action="redact").inc()
        return result

    async def _lookup_cache(
        self, span: Span, prompt: str, tenant_id: str, model: str
    ) -> CachedResponse | None:
        """Return a cached response for ``prompt``, if one is close enough."""
        if self._cache is None or not self._cache.enabled:
            return None

        lookup = await self._cache.lookup(prompt, tenant_id=tenant_id, model=model)
        span.set_attribute("distillserve.cache.similarity", lookup.similarity)
        span.set_attribute("distillserve.cache.embedder", lookup.embedder)
        span.set_attribute("distillserve.cache.semantic", lookup.semantic)
        CACHE_LOOKUPS.labels(outcome="hit" if lookup.hit else "miss").inc()
        return lookup.response if lookup.hit else None

    async def _store_in_cache(
        self, prompt: str, text: str, result: PipelineResult, tenant_id: str
    ) -> None:
        """Write a completed response to the cache.

        Only successful, untruncated completions are stored: caching a
        ``length``-truncated answer would serve a half-finished response to
        every similar prompt until it expired.
        """
        if self._cache is None or not text or result.finish_reason != "stop":
            return

        await self._cache.store(
            prompt,
            CachedResponse(
                text=text,
                model=result.route.model,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=result.cost.total_usd,
                finish_reason=result.finish_reason,
                created_at=time.time(),
            ),
            tenant_id=tenant_id,
            model=result.route.model,
        )

    async def _serve_from_cache(
        self,
        span: Span,
        cached: CachedResponse,
        redaction: RedactionResult,
        route: RouteDecision,
        completion_id: str,
        trace_id: str,
        started: float,
        fallback_reason: str | None = None,
    ) -> AsyncIterator[_Event]:
        """Emit a cached response as if it had just been generated.

        Cost is reported as **zero**, not as the original completion's cost.
        The response cost money once; serving it again costs nothing, and that
        saving is the entire reason the cache exists — booking the original
        price on every hit would erase it from the dashboard.
        """
        text = redaction.rehydrate(cached.text) if redaction.redacted else cached.text
        yield _Event(text=text)

        total = time.perf_counter() - started
        result = PipelineResult(
            usage=TokenUsage(input_tokens=cached.input_tokens, output_tokens=cached.output_tokens),
            cost=CostBreakdown(
                total_usd=0.0,
                input_usd=0.0,
                output_usd=0.0,
                basis="per_token",
                price_sheet_version=self._prices.version,
            ),
            route=route,
            metrics=GenerationMetrics(ttft_ms=total * 1_000, total_ms=total * 1_000),
            finish_reason=cached.finish_reason,
            response_id=completion_id,
            trace_id=trace_id,
            cache_hit=True,
            fallback_reason=fallback_reason,
        )
        self._annotate_response(span, result, None)
        COMPLETIONS.labels(
            route=route.target.value, backend=self._backend.name, outcome="cache_hit"
        ).inc()
        yield _Event(result=result)

    async def _degrade(
        self,
        span: Span,
        exc: BackendError,
        redaction: RedactionResult,
        route: RouteDecision,
        tenant_id: str,
        completion_id: str,
        trace_id: str,
        started: float,
    ) -> AsyncIterator[_Event] | None:
        """Serve a rate-limited request from cache instead of failing it.

        Only on a rate limit. A 400 means the request was malformed, and a
        cached answer would be answering a different question — degradation
        must never turn a client error into a plausible-looking success.
        """
        if not exc.rate_limited or self._cache is None or not self._cache.enabled:
            return None

        lookup = await self._cache.lookup(redaction.text, tenant_id=tenant_id, model=route.model)
        if lookup.response is None:
            return None

        log.warning(
            "pipeline.degraded_to_cache",
            similarity=lookup.similarity,
            model=route.model,
            reason="upstream_rate_limited",
        )
        span.set_attribute(GenAIAttributes.FALLBACK_REASON, "upstream_rate_limited")
        GUARDRAIL_EVENTS.labels(guardrail="degradation", action="cache_fallback").inc()
        return self._serve_from_cache(
            span,
            lookup.response,
            redaction,
            route,
            completion_id,
            trace_id,
            started,
            fallback_reason="upstream_rate_limited",
        )

    def _finalise(
        self,
        *,
        route: RouteDecision,
        usage: TokenUsage,
        finish_reason: str,
        response_id: str,
        trace_id: str,
        ttft: float | None,
        total: float,
    ) -> PipelineResult:
        """Assemble cost and latency figures once generation has completed."""
        # Throughput is measured over the decode window only. Including TTFT
        # would blend queueing and prefill into a decode-rate number and make
        # tok/s look worse the busier the upstream is.
        decode_seconds = max(total - (ttft or 0.0), 1e-6)
        throughput = usage.output_tokens / decode_seconds if usage.output_tokens else None

        return PipelineResult(
            usage=usage,
            cost=self._prices.hosted_cost(route.model, usage),
            route=route,
            metrics=GenerationMetrics(
                ttft_ms=None if ttft is None else ttft * 1_000,
                total_ms=total * 1_000,
                output_tokens_per_second=throughput,
            ),
            finish_reason=finish_reason,
            response_id=response_id,
            trace_id=trace_id,
            cache_hit=False,
            fallback_reason=None,
        )

    def _metadata(self, result: PipelineResult) -> DistillServeMetadata:
        """Build the ``distillserve`` block attached to a response."""
        return DistillServeMetadata(
            trace_id=result.trace_id,
            route=result.route,
            cache_hit=result.cache_hit,
            cost=result.cost,
            metrics=result.metrics,
            backend=self._backend.name,
            mode=self._mode.value,
            fallback_reason=result.fallback_reason,
        )

    @staticmethod
    def _to_backend_request(
        request: ChatCompletionRequest, route: RouteDecision, redaction: RedactionResult
    ) -> GenerationRequest:
        """Normalise an HTTP request into a backend request.

        The final user turn is replaced with its redacted form, so the provider
        never receives the personal data the redactor found. Everything else —
        system prompt, prior turns, parameters — passes through untouched.
        """
        stop = [request.stop] if isinstance(request.stop, str) else request.stop
        messages = list(request.messages)
        if redaction.redacted:
            for index in range(len(messages) - 1, -1, -1):
                if messages[index].role is ChatRole.USER:
                    messages[index] = messages[index].model_copy(update={"content": redaction.text})
                    break
        return GenerationRequest(
            model=route.model,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stop=stop,
            user=request.user,
            adapter=route.adapter,
        )

    @staticmethod
    def _latest_user_turn(request: ChatCompletionRequest) -> str:
        """Return the last user message, which is what routing classifies.

        The last turn, not the whole conversation: in a long chat the earlier
        turns are context, and classifying on all of them would make every
        request in a session inherit the first turn's label.
        """
        for message in reversed(request.messages):
            if message.role is ChatRole.USER:
                return message.content
        return request.messages[-1].content

    # -- observability ------------------------------------------------------

    def _annotate_request(
        self, span: Span, request: ChatCompletionRequest, route: RouteDecision
    ) -> None:
        """Set the request-side GenAI attributes."""
        span.set_attribute(GenAIAttributes.SYSTEM, "distillserve")
        span.set_attribute(GenAIAttributes.OPERATION_NAME, "chat")
        span.set_attribute(GenAIAttributes.REQUEST_MODEL, route.model)
        span.set_attribute(GenAIAttributes.BACKEND, self._backend.name)
        span.set_attribute(GenAIAttributes.MODE, self._mode.value)
        span.set_attribute(GenAIAttributes.ROUTE, route.target.value)
        span.set_attribute("distillserve.route.task", route.task)
        span.set_attribute("distillserve.route.reason", route.reason)
        span.set_attribute("distillserve.route.confidence", route.confidence)
        if request.max_tokens is not None:
            span.set_attribute(GenAIAttributes.REQUEST_MAX_TOKENS, request.max_tokens)
        if request.temperature is not None:
            span.set_attribute(GenAIAttributes.REQUEST_TEMPERATURE, request.temperature)
        if request.tenant is not None:
            span.set_attribute(GenAIAttributes.TENANT, request.tenant)
        if route.adapter is not None:
            span.set_attribute(GenAIAttributes.ADAPTER, route.adapter)

    @staticmethod
    def _annotate_response(span: Span, result: PipelineResult, provider_id: str | None) -> None:
        """Set the response-side GenAI attributes."""
        span.set_attribute(GenAIAttributes.RESPONSE_MODEL, result.route.model)
        span.set_attribute(GenAIAttributes.RESPONSE_ID, provider_id or result.response_id)
        span.set_attribute(GenAIAttributes.RESPONSE_FINISH_REASONS, [result.finish_reason])
        span.set_attribute(GenAIAttributes.USAGE_INPUT_TOKENS, result.usage.input_tokens)
        span.set_attribute(GenAIAttributes.USAGE_OUTPUT_TOKENS, result.usage.output_tokens)
        span.set_attribute(GenAIAttributes.COST_USD, result.cost.total_usd)
        span.set_attribute(GenAIAttributes.CACHE_HIT, result.cache_hit)
        if result.metrics.ttft_ms is not None:
            span.set_attribute(GenAIAttributes.TTFT_MS, result.metrics.ttft_ms)

    @staticmethod
    def _annotate_failure(span: Span, exc: BackendError, route: RouteDecision) -> None:
        """Record a backend failure on the span, including the fallback reason."""
        span.set_status(StatusCode.ERROR, str(exc))
        span.record_exception(exc)
        if exc.rate_limited:
            span.set_attribute(GenAIAttributes.FALLBACK_REASON, "upstream_rate_limited")
        log.warning(
            "pipeline.backend_failed",
            route=route.target.value,
            model=route.model,
            rate_limited=exc.rate_limited,
            retryable=exc.retryable,
            error=str(exc),
        )

    def _record_metrics(self, result: PipelineResult) -> None:
        """Publish the completion's counters and histograms."""
        labels = {"route": result.route.target.value, "backend": self._backend.name}
        COMPLETIONS.labels(**labels, outcome="ok").inc()
        COMPLETION_TOKENS.labels(**labels, direction="input").inc(result.usage.input_tokens)
        COMPLETION_TOKENS.labels(**labels, direction="output").inc(result.usage.output_tokens)
        COMPLETION_COST_USD.labels(**labels).inc(result.cost.total_usd)
        if result.metrics.ttft_ms is not None:
            TTFT_SECONDS.labels(**labels).observe(result.metrics.ttft_ms / 1_000)


def _new_response_id() -> str:
    """Mint an OpenAI-shaped completion id."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"
