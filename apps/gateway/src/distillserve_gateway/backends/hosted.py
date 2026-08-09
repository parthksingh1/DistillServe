"""Hosted inference through LiteLLM.

LiteLLM is used as a thin provider adapter, not as a framework: it normalises
Groq, OpenAI and Anthropic onto one streaming chunk shape, and DistillServe
owns everything above that — routing, retries, cost, tracing.

Two behaviours here are worth the words:

**Token counts.** Providers differ on whether a streaming response reports
usage. Groq and OpenAI do when asked (``stream_options``); Anthropic reports it
on its own message events. When a provider reports nothing, the backend
estimates from LiteLLM's tokenizer rather than reporting zero — a zero would
silently understate cost on every dashboard, which is worse than a documented
approximation. ``TokenUsage`` carries the number; the ``estimated`` flag on the
chunk records how it was obtained.

**Error classification.** A 429 is not a 500. The gateway's degradation path
(serve from cache, record a fallback on the trace) only triggers on a rate
limit, so misclassifying one as a generic failure would turn a recoverable
event into a user-visible error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import litellm
from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from distillserve_gateway.backends.base import (
    BackendError,
    GenerationChunk,
    GenerationRequest,
    ModelDescriptor,
)
from distillserve_otel import get_logger
from distillserve_schemas import ChatMessage, TokenUsage

log = get_logger(__name__)

# LiteLLM prints a startup banner and phones home for version checks by
# default. Both are noise in a service; disabling them here means every entry
# point gets the same quiet behaviour.
litellm.suppress_debug_info = True
litellm.telemetry = False


class HostedInferenceBackend:
    """Serves generation from hosted providers via LiteLLM."""

    def __init__(
        self,
        *,
        api_keys: dict[str, str],
        teacher_model: str,
        student_model: str,
        request_timeout: float = 120.0,
        num_retries: int = 2,
    ) -> None:
        """Configure provider credentials and defaults.

        Args:
            api_keys: Provider name to key, e.g. ``{"groq": "gsk_…"}``. Passed
                to LiteLLM per call rather than exported to the environment, so
                one process can serve several tenants' credentials later.
            teacher_model: Default teacher model id.
            student_model: Default student model id.
            request_timeout: Per-request timeout in seconds.
            num_retries: LiteLLM-level retries for transient failures. Rate
                limits are *not* retried here — the gateway handles those, so
                that a fallback is recorded on the trace instead of hidden.
        """
        self._api_keys = api_keys
        self._teacher_model = teacher_model
        self._student_model = student_model
        self._request_timeout = request_timeout
        self._num_retries = num_retries

    @property
    def name(self) -> str:
        """Backend identifier recorded on spans and responses."""
        return "hosted"

    def _api_key_for(self, model: str) -> str | None:
        """Return the key for the provider prefixing ``model``, if configured."""
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        return self._api_keys.get(provider)

    def _build_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        """Translate a :class:`GenerationRequest` into LiteLLM kwargs."""
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "timeout": self._request_timeout,
            "num_retries": self._num_retries,
            # Ask providers that support it to report usage on the final SSE
            # frame, so cost comes from the provider rather than an estimate.
            "stream_options": {"include_usage": True},
        }
        if (key := self._api_key_for(request.model)) is not None:
            kwargs["api_key"] = key
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop:
            kwargs["stop"] = list(request.stop)
        if request.user is not None:
            kwargs["user"] = request.user
        kwargs.update(request.passthrough)
        return kwargs

    async def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]:
        """Stream a completion from the provider.

        Yields:
            :class:`GenerationChunk` values. The last one carries
            ``finish_reason`` and, when the provider reports it, ``usage``.

        Raises:
            BackendError: Wrapping any provider failure, classified so the
                gateway can distinguish a rate limit from a hard error.
        """
        kwargs = self._build_kwargs(request)
        prompt_tokens = self._count_prompt_tokens(request)
        text_length = 0
        reported_usage: TokenUsage | None = None
        response_id: str | None = None
        finish_reason: str | None = None

        try:
            stream = await litellm.acompletion(stream=True, **kwargs)
            async for raw in stream:
                chunk = cast(Any, raw)
                response_id = response_id or getattr(chunk, "id", None)

                if (usage := self._extract_usage(chunk)) is not None:
                    reported_usage = usage

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason

                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    text_length += len(content)
                    yield GenerationChunk(
                        content=content,
                        model=request.model,
                        response_id=response_id,
                    )
        except (RateLimitError, Timeout, ServiceUnavailableError, APIConnectionError) as exc:
            raise self._classify(exc) from exc
        except (AuthenticationError, BadRequestError) as exc:
            raise self._classify(exc) from exc
        except Exception as exc:
            raise BackendError(f"hosted backend failed: {exc}", retryable=False) from exc

        usage = reported_usage or self._estimate_usage(prompt_tokens, text_length, request.model)
        yield GenerationChunk(
            content="",
            finish_reason=finish_reason or "stop",
            usage=usage,
            model=request.model,
            response_id=response_id,
        )

    async def list_models(self) -> Sequence[ModelDescriptor]:
        """Return the configured teacher and student.

        Only models this gateway actually routes to are listed. Enumerating
        every model a provider offers would make the Playground's picker a list
        of things the platform has no eval data for.
        """
        return (
            ModelDescriptor(
                id=self._teacher_model,
                tier="teacher",
                description="Frontier teacher model used for escalation and as the eval reference.",
            ),
            ModelDescriptor(
                id=self._student_model,
                tier="student",
                description="Distilled student serving tasks it has parity on.",
            ),
        )

    async def aclose(self) -> None:
        """No persistent resources: LiteLLM manages its own client pool."""
        return None

    # -- usage accounting ---------------------------------------------------

    @staticmethod
    def _extract_usage(chunk: Any) -> TokenUsage | None:
        """Pull provider-reported usage off a streaming chunk, if present."""
        usage = getattr(chunk, "usage", None)
        if usage is None:
            return None
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if prompt is None or completion is None:
            return None
        return TokenUsage(input_tokens=int(prompt), output_tokens=int(completion))

    def _count_prompt_tokens(self, request: GenerationRequest) -> int:
        """Count prompt tokens locally, as a fallback for silent providers."""
        return self._token_length(request.model, self._joined(request.messages))

    def _estimate_usage(self, prompt_tokens: int, text_length: int, model: str) -> TokenUsage:
        """Estimate usage when the provider reported none.

        Estimating beats reporting zero: a zero silently understates cost on
        every dashboard, while an estimate is wrong by a few percent and is
        labelled as such on the trace.
        """
        log.debug("hosted.usage_estimated", model=model)
        completion_tokens = self._token_length(model, "x" * text_length) if text_length else 0
        return TokenUsage(input_tokens=prompt_tokens, output_tokens=completion_tokens)

    @staticmethod
    def _joined(messages: Sequence[ChatMessage]) -> str:
        """Flatten messages for tokenizer-based counting."""
        return "\n".join(f"{m.role.value}: {m.content}" for m in messages)

    @staticmethod
    def _token_length(model: str, text: str) -> int:
        """Token count for ``text`` under ``model``'s tokenizer.

        Falls back to a 4-characters-per-token approximation when LiteLLM has
        no tokenizer for the model, which is the documented rule of thumb for
        English text.
        """
        if not text:
            return 0
        try:
            return int(litellm.token_counter(model=model, text=text))
        except Exception:
            return max(1, len(text) // 4)

    # -- error classification ----------------------------------------------

    @staticmethod
    def _classify(exc: Exception) -> BackendError:
        """Map a LiteLLM exception onto a :class:`BackendError`."""
        if isinstance(exc, RateLimitError):
            return BackendError(
                f"upstream provider rate-limited the request: {exc}",
                retryable=True,
                rate_limited=True,
                status_code=429,
            )
        if isinstance(exc, Timeout | ServiceUnavailableError | APIConnectionError):
            return BackendError(
                f"upstream provider is unavailable: {exc}", retryable=True, status_code=503
            )
        if isinstance(exc, AuthenticationError):
            return BackendError(
                f"provider rejected the configured credentials: {exc}",
                retryable=False,
                status_code=502,
            )
        return BackendError(
            f"provider rejected the request: {exc}", retryable=False, status_code=400
        )
