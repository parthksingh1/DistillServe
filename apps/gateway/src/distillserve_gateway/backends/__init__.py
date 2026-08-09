"""Inference backends.

Three implementations satisfy one protocol — hosted, self-hosted and sandbox —
and nothing above this package may branch on which is bound.
"""

from distillserve_gateway.backends.base import (
    BackendError,
    GenerationChunk,
    GenerationRequest,
    InferenceBackend,
    ModelDescriptor,
)
from distillserve_gateway.backends.hosted import HostedInferenceBackend

__all__ = [
    "BackendError",
    "GenerationChunk",
    "GenerationRequest",
    "HostedInferenceBackend",
    "InferenceBackend",
    "ModelDescriptor",
]
