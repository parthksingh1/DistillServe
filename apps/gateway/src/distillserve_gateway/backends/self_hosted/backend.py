"""vLLM-backed inference.

Targets a vLLM server's OpenAI-compatible ``/v1/chat/completions``. The code
path is complete and reachable from any mode the moment ``VLLM_ENDPOINT`` is
set — it is not gated behind ``DISTILLSERVE_MODE=self_hosted``, so it can be
smoke-tested against a laptop vLLM without reconfiguring the platform.

Two things differ from the hosted backend and both matter:

**Adapters are a request parameter, not a deployment.** vLLM serves multiple
LoRA adapters from one process; selecting one is just naming it as the model.
That is what makes hot-swap an API call rather than a rollout, and it is why
``GenerationRequest.adapter`` exists at all.

**Usage is always reported.** vLLM returns exact prompt and completion token
counts, so unlike the hosted path there is never an estimate — which is also
why self-hosted cost accounting can be exact where hosted sometimes cannot.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from distillserve_gateway.backends.base import (
    BackendError,
    GenerationChunk,
    GenerationRequest,
    ModelDescriptor,
)
from distillserve_otel import get_logger
from distillserve_schemas import TokenUsage

log = get_logger(__name__)


class SelfHostedInferenceBackend:
    """Streams completions from a vLLM OpenAI-compatible server."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        served_model: str | None = None,
    ) -> None:
        """Bind to a vLLM endpoint.

        Args:
            endpoint: Base URL, e.g. ``http://vllm.distillserve.svc:8000/v1``.
            api_key: Optional bearer token when vLLM is started with
                ``--api-key``.
            timeout: Request timeout. Generous by default: a long generation on
                a busy cluster legitimately takes minutes, and a short timeout
                turns queueing into spurious failures.
            served_model: Overrides the model id sent upstream. vLLM matches on
                the name it was started with, which is often a filesystem path
                rather than the id the gateway routes by.
        """
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._served_model = served_model
        self._client: Any | None = None

    @property
    def name(self) -> str:
        """Backend identifier recorded on spans and responses."""
        return "self_hosted"

    def _http(self) -> Any:
        """Return a lazily created, reused HTTP client.

        Reused because a new client per request means a new TCP and TLS
        handshake per request, which shows up directly in TTFT.
        """
        if self._client is None:
            import httpx

            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            self._client = httpx.AsyncClient(
                base_url=self._endpoint, timeout=self._timeout, headers=headers
            )
        return self._client

    def _payload(self, request: GenerationRequest) -> dict[str, Any]:
        """Build the OpenAI-compatible request body vLLM expects."""
        # An adapter *is* the model as far as vLLM is concerned: it matches the
        # name against its loaded LoRA modules first, then its base model.
        model = request.adapter or self._served_model or request.model

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
            "stream": True,
            # vLLM reports usage on the final chunk when asked; without this
            # the gateway would have to estimate, and self-hosted accounting
            # would lose the exactness that is one of its advantages.
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop"] = list(request.stop)
        payload.update(request.passthrough)
        return payload

    async def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]:
        """Stream a completion from vLLM.

        Raises:
            BackendError: on any transport or protocol failure, classified so
                the gateway's degradation path behaves the same as it does for
                hosted providers.
        """
        import httpx

        usage: TokenUsage | None = None
        finish_reason: str | None = None
        response_id: str | None = None

        try:
            async with self._http().stream(
                "POST", "/chat/completions", json=self._payload(request)
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise self._classify(response.status_code, body)

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break

                    try:
                        frame = json.loads(data)
                    except json.JSONDecodeError:
                        # A malformed frame mid-stream is not worth failing the
                        # whole generation for; the terminator still arrives.
                        log.warning("self_hosted.bad_sse_frame", preview=data[:120])
                        continue

                    response_id = response_id or frame.get("id")
                    if (raw_usage := frame.get("usage")) is not None:
                        usage = TokenUsage(
                            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                            output_tokens=int(raw_usage.get("completion_tokens", 0)),
                        )

                    for choice in frame.get("choices", []):
                        finish_reason = choice.get("finish_reason") or finish_reason
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield GenerationChunk(
                                content=content,
                                model=request.model,
                                response_id=response_id,
                            )
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"vLLM endpoint timed out after {self._timeout}s: {exc}",
                retryable=True,
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"vLLM endpoint unreachable at {self._endpoint}: {exc}",
                retryable=True,
                status_code=503,
            ) from exc

        yield GenerationChunk(
            content="",
            finish_reason=finish_reason or "stop",
            usage=usage or TokenUsage(input_tokens=0, output_tokens=0),
            model=request.model,
            response_id=response_id,
        )

    async def list_models(self) -> Sequence[ModelDescriptor]:
        """Ask vLLM what it is serving, including loaded LoRA adapters.

        Raises:
            BackendError: when the endpoint cannot be reached.
        """
        import httpx

        try:
            response = await self._http().get("/models")
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise BackendError(
                f"could not list models from {self._endpoint}: {exc}",
                retryable=True,
                status_code=503,
            ) from exc

        descriptors: list[ModelDescriptor] = []
        for entry in body.get("data", []):
            model_id = entry.get("id", "")
            # vLLM reports LoRA modules with a `parent` naming the base model,
            # which is how the gateway tells an adapter from a base checkpoint.
            is_adapter = entry.get("parent") is not None
            descriptors.append(
                ModelDescriptor(
                    id=model_id,
                    tier="adapter" if is_adapter else "student",
                    context_window=entry.get("max_model_len"),
                    description=(
                        f"LoRA adapter on {entry['parent']}" if is_adapter else "vLLM base model"
                    ),
                )
            )
        return descriptors

    async def aclose(self) -> None:
        """Close the pooled HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _classify(status_code: int, body: str) -> BackendError:
        """Map a vLLM HTTP status onto a :class:`BackendError`.

        vLLM returns 429 when its request queue is full, which is genuinely the
        same condition as a hosted provider's rate limit: retryable, and worth
        serving from cache rather than surfacing.
        """
        if status_code == 429:
            return BackendError(
                f"vLLM queue is full: {body}",
                retryable=True,
                rate_limited=True,
                status_code=429,
            )
        if status_code >= 500:
            return BackendError(
                f"vLLM server error {status_code}: {body}", retryable=True, status_code=502
            )
        return BackendError(
            f"vLLM rejected the request ({status_code}): {body}",
            retryable=False,
            status_code=status_code,
        )
