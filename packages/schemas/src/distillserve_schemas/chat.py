"""OpenAI-compatible chat contracts, plus DistillServe's routing extensions.

The request and response shapes deliberately mirror OpenAI's ``/v1/chat/completions``
so that any OpenAI SDK can point its ``base_url`` at the gateway and work
unchanged. That compatibility is the whole reason a distillation platform is
adoptable: teams migrate by changing one URL, not their application code.

Everything DistillServe adds — the route decision, cache outcome, cost — lives
under a single ``distillserve`` key on the response rather than being sprinkled
across top-level fields. A client that does not know about it ignores one
unknown key; a client that does gets everything in one place.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRole(StrEnum):
    """Author of a chat message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """One message in a conversation."""

    # `ts_client_input` marks a model the frontend *constructs* rather than
    # merely reads. The TypeScript emitter renders defaulted fields on those as
    # optional, because a client may legitimately omit them — while response
    # models keep every field required, since Pydantic always serialises them.
    model_config = ConfigDict(extra="allow", json_schema_extra={"ts_client_input": True})

    role: ChatRole
    content: str = Field(description="Message text. Multimodal parts are not yet supported.")
    name: str | None = Field(default=None, description="Optional author name.")


class RouteTarget(StrEnum):
    """Where a request may be sent.

    ``ADAPTER`` is a family rather than a single destination: the concrete LoRA
    is named in :attr:`RouteDecision.adapter`.
    """

    TEACHER = "teacher"
    STUDENT = "student"
    ADAPTER = "adapter"


class RoutePolicy(StrEnum):
    """How the caller wants the route chosen.

    ``AUTO`` defers to the task classifier. The explicit values exist because
    the Playground's Compare mode, and any A/B harness, must be able to pin a
    side of the comparison rather than argue with the router.
    """

    AUTO = "auto"
    TEACHER = "teacher"
    STUDENT = "student"


class RouteDecision(BaseModel):
    """Why a request went where it went.

    Recorded on the span and echoed to the client. A route that cannot explain
    itself is impossible to debug once it is serving production traffic, so
    ``reason`` and ``confidence`` are required rather than optional colour.
    """

    model_config = ConfigDict(frozen=True)

    target: RouteTarget
    model: str = Field(description="Concrete model id the request was sent to.")
    adapter: str | None = Field(default=None, description="LoRA adapter name, when applicable.")
    task: str = Field(description="Task label assigned by the classifier, e.g. 'summarization'.")
    policy: RoutePolicy = Field(description="Policy that produced this decision.")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence in ``task``.")
    reason: str = Field(description="Human-readable justification, shown in the trace waterfall.")


class TokenUsage(BaseModel):
    """Token counts for one completion."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        """Sum of input and output tokens."""
        return self.input_tokens + self.output_tokens


class CostBreakdown(BaseModel):
    """What a completion cost, and how that was computed.

    Both hosted and self-hosted costs land in ``total_usd`` so every dashboard
    and eval can compare them directly — that comparison is the platform's
    central claim, and it only works if one number means the same thing in both
    modes. ``basis`` records which formula produced it.
    """

    model_config = ConfigDict(frozen=True)

    total_usd: float = Field(ge=0.0)
    input_usd: float = Field(ge=0.0)
    output_usd: float = Field(ge=0.0)
    basis: Literal["per_token", "per_gpu_second"] = Field(
        description="Hosted providers bill per token; self-hosted serving bills per GPU-second."
    )
    price_sheet_version: str = Field(description="Version of prices.yaml used, for auditability.")


class GenerationMetrics(BaseModel):
    """Latency measurements for one completion.

    TTFT and ITL are reported separately because they answer different product
    questions: TTFT is what a user perceives as responsiveness, ITL is what
    determines whether a long answer feels smooth. An average of the two hides
    both.
    """

    model_config = ConfigDict(frozen=True)

    ttft_ms: float | None = Field(
        default=None, ge=0.0, description="Time to first token; null for non-streaming calls."
    )
    total_ms: float = Field(ge=0.0, description="Wall-clock duration of the completion.")
    output_tokens_per_second: float | None = Field(
        default=None, ge=0.0, description="Decode throughput, once at least one token has arrived."
    )


class DistillServeMetadata(BaseModel):
    """DistillServe's additions to an OpenAI-shaped response."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(description="OTel trace id, for deep-linking into Langfuse.")
    route: RouteDecision
    cache_hit: bool = Field(description="Whether the semantic cache served this response.")
    cost: CostBreakdown
    metrics: GenerationMetrics
    backend: str = Field(description="Backend that served generation, e.g. 'hosted'.")
    mode: str = Field(description="Deployment mode in effect.")
    fallback_reason: str | None = Field(
        default=None,
        description="Set when the primary path failed and a fallback served the request.",
    )


class ChatCompletionRequest(BaseModel):
    """Request body for ``POST /v1/chat/completions``.

    ``extra="allow"`` so that OpenAI parameters this gateway does not yet
    interpret (``tools``, ``response_format``, …) survive the round trip to the
    provider instead of being silently dropped.
    """

    model_config = ConfigDict(extra="allow", json_schema_extra={"ts_client_input": True})

    model: str | None = Field(
        default=None,
        description="Model id. Omit to let the router choose; a concrete id pins the route.",
    )
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = Field(default=False)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stop: list[str] | str | None = Field(default=None)
    user: str | None = Field(default=None, description="Opaque end-user id, forwarded upstream.")

    # --- DistillServe extensions -------------------------------------------
    route_policy: RoutePolicy = Field(
        default=RoutePolicy.AUTO,
        description="Override the router. `auto` uses the task classifier.",
    )
    task: str | None = Field(
        default=None,
        description="Skip classification by declaring the task yourself.",
    )
    tenant: str | None = Field(
        default=None,
        description="Tenant id for rate limiting and per-tenant cost attribution.",
    )


class ChatCompletionChoice(BaseModel):
    """One completion choice in a non-streaming response."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    message: ChatMessage
    finish_reason: str | None = Field(default=None)


class ChatCompletionResponse(BaseModel):
    """Non-streaming response body, OpenAI-shaped plus ``distillserve``."""

    model_config = ConfigDict(frozen=True)

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(description="Unix timestamp, seconds.")
    model: str
    choices: list[ChatCompletionChoice]
    usage: TokenUsage
    distillserve: DistillServeMetadata


class ChatCompletionDelta(BaseModel):
    """Incremental message content in a streaming chunk."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole | None = Field(default=None)
    content: str | None = Field(default=None)


class ChatCompletionChunkChoice(BaseModel):
    """One choice within a streaming chunk."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=0)
    delta: ChatCompletionDelta
    finish_reason: str | None = Field(default=None)


class ChatCompletionChunk(BaseModel):
    """One SSE frame, OpenAI-shaped.

    ``distillserve`` is attached only to the final frame: usage, cost and TTFT
    are not known until the stream ends, and emitting placeholder values on
    every frame would invite a client to read them mid-stream.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: TokenUsage | None = Field(default=None)
    distillserve: DistillServeMetadata | None = Field(default=None)


class GatewayErrorBody(BaseModel):
    """Structured error payload.

    Shaped like OpenAI's error envelope so a client's existing error handling
    keeps working, with ``fallback_reason`` added for the degradation case: a
    provider rate limit that could not be served from cache is a different
    operational event from a malformed request, and the trace records which.
    """

    model_config = ConfigDict(frozen=True)

    message: str
    type: str = Field(description="Error class, e.g. 'rate_limit_error'.")
    code: str | None = Field(default=None)
    trace_id: str | None = Field(default=None)
    fallback_reason: str | None = Field(default=None)


class GatewayError(BaseModel):
    """Error response envelope."""

    model_config = ConfigDict(frozen=True)

    error: GatewayErrorBody
