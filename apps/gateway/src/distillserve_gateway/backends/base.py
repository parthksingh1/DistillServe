"""The ``InferenceBackend`` protocol every serving path implements.

Three backends exist — hosted, self-hosted and sandbox — and the platform's
central design constraint is that they are *drop-in equivalent*. Nothing above
this interface may branch on which one is bound; switching
``DISTILLSERVE_MODE`` changes one line of wiring in the app factory and nothing
else.

The protocol is intentionally narrow. Generation is expressed as a single
streaming method, because a non-streaming response is a stream collected to
completion — modelling it the other way around forces every backend to
implement the hard case twice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from distillserve_schemas import ChatMessage, TokenUsage


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A normalised generation request.

    Deliberately not the HTTP request model: by the time a backend sees this,
    routing, guardrails and cache lookup have already run, and the model id is
    resolved. Keeping the two types separate stops backend code from having to
    care about DistillServe's routing extensions.
    """

    model: str
    messages: Sequence[ChatMessage]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: Sequence[str] | None = None
    user: str | None = None
    adapter: str | None = None
    #: Provider parameters passed through untouched (``tools``, ``response_format``…).
    passthrough: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationChunk:
    """One increment of a generation.

    ``usage`` and ``finish_reason`` arrive on the final chunk only. A chunk
    with empty ``content`` and a ``finish_reason`` is the normal terminator.
    """

    content: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    model: str | None = None
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """A model a backend can serve."""

    id: str
    tier: str = "teacher"
    context_window: int | None = None
    adapters: tuple[str, ...] = ()
    description: str = ""


class BackendError(RuntimeError):
    """A backend failed to serve a request.

    ``retryable`` and ``rate_limited`` are separate flags because they drive
    different behaviour: a rate limit makes the gateway try the cache and
    record a fallback on the trace, while a transient network error is simply
    retried.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        rate_limited: bool = False,
        status_code: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Record why the backend failed and how the caller should react."""
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.status_code = status_code
        #: Set by the pipeline as the error propagates, so the error body can
        #: name the trace even though the span has already closed by the time
        #: the route handler renders it.
        self.trace_id = trace_id


@runtime_checkable
class InferenceBackend(Protocol):
    """What every serving path must provide."""

    @property
    def name(self) -> str:
        """Short identifier recorded on spans and responses, e.g. ``hosted``."""
        ...

    def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]:
        """Stream a completion.

        Implementations are async generators. The final chunk carries
        ``finish_reason`` and, where the provider reports it, ``usage``.
        """
        ...

    async def list_models(self) -> Sequence[ModelDescriptor]:
        """Return the models this backend can serve."""
        ...

    async def aclose(self) -> None:
        """Release any long-lived resources held by the backend."""
        ...
