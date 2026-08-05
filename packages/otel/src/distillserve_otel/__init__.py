"""Shared observability setup for DistillServe services."""

from distillserve_otel.logging import configure_logging, get_logger
from distillserve_otel.tracing import GenAIAttributes, configure_tracing, shutdown_tracing

__all__ = [
    "GenAIAttributes",
    "configure_logging",
    "configure_tracing",
    "get_logger",
    "shutdown_tracing",
]
