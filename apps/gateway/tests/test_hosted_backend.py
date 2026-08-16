"""Tests for the LiteLLM-backed hosted inference backend.

``litellm.acompletion`` is patched rather than called, so these tests cover the
translation and accounting logic — kwargs, usage extraction, error
classification — without a network or an API key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm
import pytest
from litellm.exceptions import AuthenticationError, BadRequestError, RateLimitError, Timeout

from distillserve_gateway.backends.base import BackendError, GenerationChunk, GenerationRequest
from distillserve_gateway.backends.hosted import HostedInferenceBackend
from distillserve_schemas import ChatMessage, ChatRole

TEACHER = "groq/llama-3.3-70b-versatile"
STUDENT = "groq/llama-3.1-8b-instant"


def make_backend() -> HostedInferenceBackend:
    return HostedInferenceBackend(
        api_keys={"groq": "gsk-test", "openai": "sk-test"},
        teacher_model=TEACHER,
        student_model=STUDENT,
    )


def make_request(**overrides: Any) -> GenerationRequest:
    defaults: dict[str, Any] = {
        "model": STUDENT,
        "messages": [ChatMessage(role=ChatRole.USER, content="Summarize this thread.")],
    }
    defaults.update(overrides)
    return GenerationRequest(**defaults)


class _Delta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Chunk:
    def __init__(
        self,
        content: str | None = None,
        finish_reason: str | None = None,
        usage: _Usage | None = None,
        chunk_id: str = "prov-1",
    ) -> None:
        self.id = chunk_id
        self.choices = [_Choice(content, finish_reason)] if content is not None else []
        self.usage = usage


class FakeCompletion:
    """Stands in for ``litellm.acompletion``.

    A class rather than a closure so the kwargs it was called with are a real
    attribute, which both the tests and the type checker can see.
    """

    def __init__(self, *chunks: _Chunk, error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> AsyncIterator[_Chunk]:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error

        async def _iter() -> AsyncIterator[_Chunk]:
            for chunk in self.chunks:
                yield chunk

        return _iter()


def stream_of(*chunks: _Chunk) -> FakeCompletion:
    """Build a fake completion returning ``chunks`` as an async iterator."""
    return FakeCompletion(*chunks)


def raising(exc: Exception) -> FakeCompletion:
    """Build a fake completion that raises ``exc``."""
    return FakeCompletion(error=exc)


async def collect(
    backend: HostedInferenceBackend, request: GenerationRequest
) -> list[GenerationChunk]:
    """Drain a backend's stream into a list."""
    return [chunk async for chunk in backend.generate(request)]


# --- happy path ------------------------------------------------------------


async def test_streams_content_then_a_terminal_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        stream_of(
            _Chunk("Hello"),
            _Chunk(", world"),
            _Chunk("", finish_reason="stop", usage=_Usage(42, 3)),
        ),
    )

    chunks = await collect(make_backend(), make_request())

    assert "".join(c.content for c in chunks) == "Hello, world"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_tokens == 42
    assert chunks[-1].usage.output_tokens == 3


async def test_provider_reported_usage_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        stream_of(_Chunk("Hi"), _Chunk("", finish_reason="stop", usage=_Usage(101, 7))),
    )

    chunks = await collect(make_backend(), make_request())

    usage = chunks[-1].usage
    assert usage is not None
    assert usage.input_tokens == 101


async def test_usage_is_estimated_when_the_provider_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reporting zero would silently understate cost on every dashboard."""
    monkeypatch.setattr(
        litellm, "acompletion", stream_of(_Chunk("Hello there"), _Chunk("", finish_reason="stop"))
    )

    chunks = await collect(make_backend(), make_request())
    usage = chunks[-1].usage

    assert usage is not None
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0


async def test_request_parameters_are_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = stream_of(_Chunk("x"), _Chunk("", finish_reason="stop", usage=_Usage(1, 1)))
    monkeypatch.setattr(litellm, "acompletion", fake)

    await collect(
        make_backend(),
        make_request(max_tokens=64, temperature=0.3, top_p=0.9, stop=["END"], user="tenant-1"),
    )

    kwargs = fake.kwargs
    assert kwargs["model"] == STUDENT
    assert kwargs["max_tokens"] == 64
    assert kwargs["temperature"] == 0.3
    assert kwargs["top_p"] == 0.9
    assert kwargs["stop"] == ["END"]
    assert kwargs["user"] == "tenant-1"
    assert kwargs["messages"] == [{"role": "user", "content": "Summarize this thread."}]
    # Usage on the final SSE frame is what keeps cost accounting exact.
    assert kwargs["stream_options"] == {"include_usage": True}


async def test_the_providers_key_is_selected_from_the_model_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = stream_of(_Chunk("x"), _Chunk("", finish_reason="stop", usage=_Usage(1, 1)))
    monkeypatch.setattr(litellm, "acompletion", fake)

    await collect(make_backend(), make_request(model="openai/gpt-4o"))

    assert fake.kwargs["api_key"] == "sk-test"


async def test_unknown_provider_sends_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = stream_of(_Chunk("x"), _Chunk("", finish_reason="stop", usage=_Usage(1, 1)))
    monkeypatch.setattr(litellm, "acompletion", fake)

    await collect(make_backend(), make_request(model="mistral/mistral-large"))

    assert "api_key" not in fake.kwargs


async def test_passthrough_parameters_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI parameters the gateway does not interpret must not be dropped."""
    fake = stream_of(_Chunk("x"), _Chunk("", finish_reason="stop", usage=_Usage(1, 1)))
    monkeypatch.setattr(litellm, "acompletion", fake)

    await collect(
        make_backend(), make_request(passthrough={"response_format": {"type": "json_object"}})
    )

    assert fake.kwargs["response_format"] == {"type": "json_object"}


async def test_list_models_returns_only_routed_models() -> None:
    models = await make_backend().list_models()

    assert {m.id for m in models} == {TEACHER, STUDENT}
    assert {m.tier for m in models} == {"teacher", "student"}


# --- error classification --------------------------------------------------


async def test_rate_limits_are_classified_as_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degradation path keys off this flag; misclassifying it breaks fallback."""
    monkeypatch.setattr(
        litellm,
        "acompletion",
        raising(RateLimitError("slow down", llm_provider="groq", model=STUDENT)),
    )

    with pytest.raises(BackendError) as caught:
        await collect(make_backend(), make_request())

    assert caught.value.rate_limited is True
    assert caught.value.retryable is True
    assert caught.value.status_code == 429


async def test_timeouts_are_retryable_but_not_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        raising(Timeout("too slow", llm_provider="groq", model=STUDENT)),
    )

    with pytest.raises(BackendError) as caught:
        await collect(make_backend(), make_request())

    assert caught.value.rate_limited is False
    assert caught.value.retryable is True


async def test_authentication_failures_are_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        raising(AuthenticationError("bad key", llm_provider="groq", model=STUDENT)),
    )

    with pytest.raises(BackendError) as caught:
        await collect(make_backend(), make_request())

    assert caught.value.retryable is False
    assert caught.value.status_code == 502


async def test_bad_requests_are_reported_as_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        raising(BadRequestError("nope", llm_provider="groq", model=STUDENT)),
    )

    with pytest.raises(BackendError) as caught:
        await collect(make_backend(), make_request())

    assert caught.value.retryable is False
    assert caught.value.status_code == 400


async def test_unexpected_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider SDKs raise freely; nothing may escape as a raw exception."""
    monkeypatch.setattr(litellm, "acompletion", raising(ValueError("something odd")))

    with pytest.raises(BackendError, match="hosted backend failed"):
        await collect(make_backend(), make_request())
