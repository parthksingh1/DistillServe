"""Self-hosted serving: vLLM behind an OpenAI-compatible endpoint."""

from distillserve_gateway.backends.self_hosted.backend import SelfHostedInferenceBackend

__all__ = ["SelfHostedInferenceBackend"]
