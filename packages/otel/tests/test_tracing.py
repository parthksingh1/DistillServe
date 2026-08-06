"""Tests for the shared observability setup."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from distillserve_otel import GenAIAttributes, configure_logging, configure_tracing, get_logger
from distillserve_otel.tracing import _langfuse_headers, shutdown_tracing


def test_langfuse_headers_are_basic_auth_over_the_key_pair() -> None:
    headers = _langfuse_headers("pk-lf-1", "sk-lf-1")
    scheme, _, token = headers["Authorization"].partition(" ")

    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "pk-lf-1:sk-lf-1"


def test_tracing_installs_a_provider_without_an_exporter() -> None:
    """No credentials must still yield a usable tracer, so call sites stay unconditional."""
    tracer = configure_tracing(
        service_name="test-service", service_version="0.1.0", environment="test"
    )
    try:
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "test-service"

        with tracer.start_as_current_span("unit") as span:
            span.set_attribute(GenAIAttributes.ROUTE, "student")
            assert span.get_span_context().is_valid
    finally:
        shutdown_tracing()


def test_logging_emits_json_with_trace_context(capsys: Any) -> None:
    tracer = configure_tracing(
        service_name="test-service", service_version="0.1.0", environment="test"
    )
    configure_logging(level="info", json_logs=True)
    try:
        log = get_logger("test")
        with tracer.start_as_current_span("unit"):
            log.info("hello", route="teacher")

        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert record["event"] == "hello"
        assert record["route"] == "teacher"
        assert record["level"] == "info"
        # The trace id is what joins this line to its Langfuse span.
        assert len(record["trace_id"]) == 32
    finally:
        shutdown_tracing()
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()
