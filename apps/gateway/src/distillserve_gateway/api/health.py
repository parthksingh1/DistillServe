"""Operational endpoints: ``/healthz``, ``/readyz`` and ``/metrics``.

The split between liveness and readiness is deliberate and load-bearing:

* ``/healthz`` touches nothing external. Render restarts a container whose
  liveness probe fails, so making liveness depend on Redis would turn a Redis
  blip into a fleet-wide restart storm.
* ``/readyz`` runs the dependency probes and is what a load balancer should
  poll. It returns 503 when a *required* dependency is down, and 200 with
  ``status: "degraded"`` when only optional ones are.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from distillserve_gateway import __version__
from distillserve_gateway.core import metrics, readiness
from distillserve_gateway.core.build_info import git_sha, git_sha_short
from distillserve_gateway.core.settings import Settings, get_settings
from distillserve_schemas import (
    HealthStatus,
    LivenessResponse,
    ReadinessResponse,
    ServiceIdentity,
)

router = APIRouter(tags=["operations"])

#: Process start time, captured at import so uptime is measured from boot.
_STARTED_AT = time.monotonic()

SettingsDep = Annotated[Settings, Depends(get_settings)]


def build_identity(settings: Settings) -> ServiceIdentity:
    """Assemble the service identity echoed by both probes."""
    return ServiceIdentity(
        service=settings.service_name,
        version=__version__,
        git_sha=git_sha(),
        git_sha_short=git_sha_short(),
        mode=settings.mode,
        environment=settings.environment,
    )


@router.get(
    "/healthz",
    response_model=LivenessResponse,
    summary="Liveness probe",
    description="Process-local health. Never touches a dependency.",
)
async def healthz(settings: SettingsDep) -> LivenessResponse:
    """Return the process's liveness, build identity and uptime."""
    return LivenessResponse(
        status=HealthStatus.OK,
        identity=build_identity(settings),
        uptime_seconds=time.monotonic() - _STARTED_AT,
    )


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A required dependency is down."}},
)
async def readyz(settings: SettingsDep, response: Response) -> ReadinessResponse:
    """Run every dependency probe concurrently and fold the results."""
    checks = list(await asyncio.gather(*(probe(settings) for probe in readiness.DEFAULT_PROBES)))
    overall = readiness.aggregate(checks)
    if overall is HealthStatus.FAILED:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status=overall, identity=build_identity(settings), checks=checks)


@router.get(
    "/metrics",
    summary="Prometheus scrape endpoint",
    response_class=Response,
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    """Render the gateway's private collector registry in Prometheus text format."""
    return Response(content=generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)
