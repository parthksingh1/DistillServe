"""``python -m distillserve_gateway`` — run the gateway with uvicorn."""

from __future__ import annotations

import uvicorn

from distillserve_gateway.core.settings import get_settings


def run() -> None:
    """Serve the gateway using host/port from settings."""
    settings = get_settings()
    uvicorn.run(
        "distillserve_gateway.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,  # structlog owns logging; uvicorn's dictConfig would undo it.
    )


if __name__ == "__main__":
    run()
