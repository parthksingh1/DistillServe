"""ASGI entrypoint.

``uvicorn distillserve_gateway.main:app`` is what Render, Docker and
``make dev`` all run, so the module stays a thin, import-safe shim over
:func:`create_app`.
"""

from __future__ import annotations

from fastapi import FastAPI

from distillserve_gateway.app import create_app

app: FastAPI = create_app()
