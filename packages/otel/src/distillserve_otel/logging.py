"""structlog configuration shared by every DistillServe service.

Two renderers, one pipeline: a console renderer for local development and a JSON
renderer everywhere else. The processor chain is identical in both cases so a
log line that is legible locally is also parseable in Render's log drain.

The chain also injects the active trace/span id when one is in scope, which is
what makes a Langfuse trace and a log line joinable after the fact.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

_LEVELS: dict[str, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def _add_trace_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the current OTel trace and span ids to the log record, if any."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def configure_logging(*, level: str = "info", json_logs: bool = True) -> None:
    """Install the structlog pipeline process-wide.

    Idempotent: calling it twice (app factory plus a worker entrypoint) simply
    reinstalls the same configuration.

    Args:
        level: Minimum level name, case-insensitive.
        json_logs: Emit JSON when true, human-readable console output otherwise.
    """
    numeric_level = _LEVELS.get(level.lower(), logging.INFO)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_trace_context,
    ]
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, httpx, litellm) through the same sink so
    # there is exactly one log format in production. `force=True` is what lets
    # this run *after* uvicorn has installed its own handlers: it replaces
    # them, so no `--log-config` flag is needed at any call site (and none
    # would be portable — `/dev/null` does not exist on Windows).
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level, force=True)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger that tags every record with ``name``.

    The name is passed as an initial value rather than added by
    ``structlog.stdlib.add_logger_name``: that processor reads ``logger.name``,
    which only exists on stdlib loggers, and this pipeline writes through
    ``PrintLoggerFactory`` to avoid a stdlib round-trip on the hot path.

    ``structlog.get_logger`` returns a *lazy* proxy, which matters: module-level
    loggers are created at import time, before :func:`configure_logging` runs,
    and a proxy picks up the real configuration on first use instead of
    freezing the defaults.
    """
    # The key is `logger_name`, not `logger`: the latter is a positional
    # parameter of structlog's `wrap_logger` and collides with it.
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(logger_name=name)
    return logger
