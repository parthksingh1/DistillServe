"""``SandboxInferenceBackend`` — hosted generation, reference telemetry.

Sandbox mode exists so the platform's operational surfaces — dashboards,
rollout controls, eval reports — can be evaluated without provisioning GPUs.
It is not a stub of the platform: routing, caching, guardrails, cost
accounting, tracing and the eval harness all run exactly as they do in
`hosted` mode, because generation genuinely goes through the hosted backend.

What differs is one thing: ``get_telemetry()`` returns
:class:`SandboxTelemetry` instead of :class:`SelfHostedTelemetry`. Everything
downstream of that boundary is shared code.

The delegation is explicit rather than inherited. Subclassing
``HostedInferenceBackend`` would let a future change to the hosted backend
silently alter sandbox behaviour; wrapping it keeps the seam visible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from distillserve_gateway.backends.base import (
    GenerationChunk,
    GenerationRequest,
    InferenceBackend,
    ModelDescriptor,
)
from distillserve_gateway.backends.telemetry import SandboxTelemetry, TelemetryStream


class SandboxInferenceBackend:
    """Delegates generation to a hosted backend; serves reference telemetry."""

    def __init__(self, generation: InferenceBackend, telemetry: SandboxTelemetry) -> None:
        """Compose a real generation backend with the reference telemetry stream.

        Args:
            generation: Where completions actually come from — in practice
                :class:`HostedInferenceBackend`, so the Playground is live.
            telemetry: The reference-dataset telemetry stream.
        """
        self._generation = generation
        self._telemetry = telemetry

    @property
    def name(self) -> str:
        """Backend identifier recorded on spans and responses."""
        return "sandbox"

    @property
    def generation_backend(self) -> str:
        """Which backend is actually generating.

        Surfaced in the UI so nobody has to guess where a Playground response
        came from: it says `sandbox (generation: hosted)`.
        """
        return self._generation.name

    def generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]:
        """Stream a completion from the underlying hosted backend."""
        return self._generation.generate(request)

    async def list_models(self) -> Sequence[ModelDescriptor]:
        """List the models the underlying backend serves."""
        return await self._generation.list_models()

    def get_telemetry(self) -> TelemetryStream:
        """Return the reference telemetry stream."""
        return self._telemetry

    async def aclose(self) -> None:
        """Release the underlying backend's resources."""
        await self._generation.aclose()
