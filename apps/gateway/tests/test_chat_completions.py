"""End-to-end tests for ``/v1/chat/completions``.

A stub backend stands in for LiteLLM so these tests exercise the real pipeline,
the real router and the real cost math against a deterministic token stream.
The provider client itself is covered separately in ``test_hosted_backend.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from distillserve_gateway.app import create_app
from distillserve_gateway.backends.base import (
    BackendError,
    GenerationChunk,
    GenerationRequest,
    ModelDescriptor,
)
from distillserve_gateway.bootstrap import build_router
from distillserve_gateway.core.pricing import load_price_sheet
from distillserve_gateway.pipeline import CompletionPipeline
from distillserve_schemas import TokenUsage

from .conftest import make_settings

TEACHER = "groq/llama-3.3-70b-versatile"
STUDENT = "groq/llama-3.1-8b-instant"


class StubBackend:
    """Emits a fixed token stream, or raises a configured BackendError."""

    def __init__(
        self,
        pieces: Sequence[str] = ("Hello", ", ", "world"),
        usage: TokenUsage | None = None,
        error: BackendError | None = None,
    ) -> None:
        self.pieces = pieces
        self.usage = usage or TokenUsage(input_tokens=42, output_tokens=3)
        self.error = error
        self.seen: list[GenerationRequest] = []

    @property
    def name(self) -> str:
        return "stub"

    async def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        for piece in self.pieces:
            yield GenerationChunk(content=piece, model=request.model, response_id="prov-1")
        yield GenerationChunk(
            content="", finish_reason="stop", usage=self.usage, model=request.model
        )

    async def list_models(self) -> Sequence[ModelDescriptor]:
        return (ModelDescriptor(id=TEACHER), ModelDescriptor(id=STUDENT))

    async def aclose(self) -> None:
        return None


def build_app(backend: StubBackend) -> FastAPI:
    settings = make_settings()
    app = create_app(settings)
    app.state.pipeline = CompletionPipeline(
        backend=backend,
        router=build_router(settings),
        price_sheet=load_price_sheet(),
        mode=settings.mode,
    )
    return app


async def call(app: FastAPI, payload: dict[str, object]) -> Response:
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            return await http.post("/v1/chat/completions", json=payload)


def _delta(frame: dict[str, object]) -> dict[str, object]:
    """Pull the delta object out of an SSE frame, narrowing for the type checker."""
    choices = cast(list[dict[str, Any]], frame["choices"])
    return cast(dict[str, object], choices[0]["delta"])


def sse_frames(text: str) -> list[dict[str, object]]:
    """Parse an SSE body into decoded JSON frames, excluding the terminator."""
    frames: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ").strip()
        if payload == "[DONE]":
            continue
        frames.append(json.loads(payload))
    return frames


# --- non-streaming ---------------------------------------------------------


async def test_returns_an_openai_shaped_response() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this thread."}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"]["content"] == "Hello, world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"input_tokens": 42, "output_tokens": 3}


async def test_response_carries_route_cost_and_metrics() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this support thread."}]},
    )

    meta = response.json()["distillserve"]
    assert meta["route"]["target"] == "student"
    assert meta["route"]["task"] == "summarization"
    assert meta["route"]["reason"]
    assert meta["cache_hit"] is False
    assert meta["backend"] == "stub"
    assert meta["mode"] == "hosted"
    assert meta["cost"]["total_usd"] > 0.0
    assert meta["cost"]["basis"] == "per_token"
    assert meta["metrics"]["total_ms"] >= 0.0
    assert len(meta["trace_id"]) == 32


async def test_cost_matches_the_price_sheet() -> None:
    """The number on the dashboard must be re-derivable from prices.yaml."""
    usage = TokenUsage(input_tokens=1_000, output_tokens=500)
    response = await call(
        build_app(StubBackend(usage=usage)),
        {"messages": [{"role": "user", "content": "Summarize this."}]},
    )

    expected = load_price_sheet().hosted_cost(STUDENT, usage)
    assert response.json()["distillserve"]["cost"]["total_usd"] == pytest.approx(expected.total_usd)


async def test_route_policy_pins_the_teacher() -> None:
    backend = StubBackend()
    await call(
        build_app(backend),
        {
            "messages": [{"role": "user", "content": "Summarize this."}],
            "route_policy": "teacher",
        },
    )

    assert backend.seen[0].model == TEACHER


async def test_explicit_model_is_forwarded_to_the_backend() -> None:
    backend = StubBackend()
    await call(
        build_app(backend),
        {"model": TEACHER, "messages": [{"role": "user", "content": "Summarize this."}]},
    )

    assert backend.seen[0].model == TEACHER


async def test_routing_classifies_the_latest_user_turn() -> None:
    """Earlier turns are context; classifying on all of them mislabels a session."""
    response = await call(
        build_app(StubBackend()),
        {
            "messages": [
                {"role": "user", "content": "Write a poem about the sea."},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "Now summarize it in two sentences."},
            ]
        },
    )

    assert response.json()["distillserve"]["route"]["task"] == "summarization"


async def test_generation_parameters_reach_the_backend() -> None:
    backend = StubBackend()
    await call(
        build_app(backend),
        {
            "messages": [{"role": "user", "content": "Summarize this."}],
            "max_tokens": 128,
            "temperature": 0.2,
            "stop": "END",
        },
    )

    seen = backend.seen[0]
    assert seen.max_tokens == 128
    assert seen.temperature == 0.2
    assert seen.stop == ["END"]


async def test_empty_message_list_is_rejected() -> None:
    response = await call(build_app(StubBackend()), {"messages": []})
    assert response.status_code == 422


# --- streaming -------------------------------------------------------------


async def test_streaming_emits_openai_sse_frames() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("data: [DONE]\n\n")

    frames = sse_frames(response.text)
    content = "".join(str(_delta(frame).get("content", "")) for frame in frames)
    assert content == "Hello, world"
    assert all(frame["object"] == "chat.completion.chunk" for frame in frames)


async def test_every_frame_shares_one_response_id() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    ids = {frame["id"] for frame in sse_frames(response.text)}
    assert len(ids) == 1
    assert str(next(iter(ids))).startswith("chatcmpl-")


async def test_only_the_first_frame_declares_the_role() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    frames = sse_frames(response.text)
    roles = [_delta(frame).get("role") for frame in frames]
    assert roles[0] == "assistant"
    assert all(role is None for role in roles[1:-1])


async def test_terminal_frame_carries_usage_and_metadata() -> None:
    """Usage is not known until the stream ends, so it lands on the last frame only."""
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    frames = sse_frames(response.text)
    terminal = frames[-1]
    assert terminal["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]
    assert terminal["usage"] == {"input_tokens": 42, "output_tokens": 3}
    assert terminal["distillserve"]["cost"]["total_usd"] > 0  # type: ignore[index]
    assert "usage" not in frames[0]


async def test_streaming_reports_ttft() -> None:
    response = await call(
        build_app(StubBackend()),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    metrics = sse_frames(response.text)[-1]["distillserve"]["metrics"]  # type: ignore[index]
    assert metrics["ttft_ms"] is not None
    assert metrics["ttft_ms"] <= metrics["total_ms"]


# --- failure handling ------------------------------------------------------


async def test_rate_limit_returns_429_with_a_fallback_reason() -> None:
    backend = StubBackend(error=BackendError("upstream busy", rate_limited=True, status_code=429))
    response = await call(
        build_app(backend), {"messages": [{"role": "user", "content": "Summarize this."}]}
    )

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["fallback_reason"] == "upstream_rate_limited"
    assert len(error["trace_id"]) == 32


async def test_transient_failure_returns_503() -> None:
    backend = StubBackend(error=BackendError("provider down", retryable=True, status_code=503))
    response = await call(
        build_app(backend), {"messages": [{"role": "user", "content": "Summarize this."}]}
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable_error"


async def test_streaming_failure_is_delivered_as_a_final_frame() -> None:
    """Headers are already sent, so the status cannot change — the frame must carry it."""
    backend = StubBackend(error=BackendError("upstream busy", rate_limited=True, status_code=429))
    response = await call(
        build_app(backend),
        {"messages": [{"role": "user", "content": "Summarize this."}], "stream": True},
    )

    assert response.status_code == 200
    frames = sse_frames(response.text)
    assert frames[-1]["error"]["type"] == "rate_limit_error"  # type: ignore[index]
    assert response.text.endswith("data: [DONE]\n\n")


async def test_completions_are_counted_in_metrics() -> None:
    app = build_app(StubBackend())
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            await http.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Summarize this."}]},
            )
            metrics = (await http.get("/metrics")).text

    assert 'distillserve_completions_total{backend="stub"' in metrics
    assert "distillserve_completion_cost_usd_total" in metrics
