"""HTTP surface for the platform services.

Dashboards, adapters, rollouts, the model registry and eval reports. Read
endpoints require the ``read`` scope; anything that changes platform state
requires ``operate``, which is what keeps a demo visitor from advancing a
rollout.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from distillserve_gateway.api.deps import RequireOperate, RequireRead
from distillserve_gateway.backends.telemetry import TelemetryStream
from distillserve_gateway.platform.registry import PlatformStore
from distillserve_gateway.platform.rollouts import RolloutController, RolloutTransitionError
from distillserve_otel import get_logger
from distillserve_schemas import (
    Adapter,
    ClusterEvent,
    EvalReport,
    ModelVersion,
    Rollout,
    RolloutEvent,
    ServingSnapshot,
    TenantUsage,
    TimeSeries,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["platform"])


# --- dependencies ----------------------------------------------------------


def get_telemetry(request: Request) -> TelemetryStream:
    """Return the telemetry stream bound to this app."""
    stream = getattr(request.app.state, "telemetry", None)
    if stream is None:  # pragma: no cover - misconfiguration fails at startup
        raise RuntimeError("No telemetry stream is bound to this application.")
    return stream  # type: ignore[no-any-return]


def get_store(request: Request) -> PlatformStore:
    """Return the platform store bound to this app."""
    store = getattr(request.app.state, "platform_store", None)
    if store is None:  # pragma: no cover
        raise RuntimeError("No platform store is bound to this application.")
    return store  # type: ignore[no-any-return]


def get_controller(request: Request) -> RolloutController:
    """Return the rollout controller bound to this app."""
    controller = getattr(request.app.state, "rollout_controller", None)
    if controller is None:  # pragma: no cover
        raise RuntimeError("No rollout controller is bound to this application.")
    return controller  # type: ignore[no-any-return]


TelemetryDep = Annotated[TelemetryStream, Depends(get_telemetry)]
StoreDep = Annotated[PlatformStore, Depends(get_store)]
ControllerDep = Annotated[RolloutController, Depends(get_controller)]
WindowQuery = Annotated[str, Query(pattern="^(1h|24h|7d)$")]


# --- response bodies -------------------------------------------------------


class TelemetryInfo(BaseModel):
    """Which telemetry stream is active and where its numbers come from."""

    model_config = ConfigDict(frozen=True)

    stream: str
    provenance: str = Field(
        description="Human-readable origin, shown next to the numbers in the UI."
    )


class DashboardSummary(BaseModel):
    """Everything the Live Dashboard needs for its first paint."""

    model_config = ConfigDict(frozen=True)

    telemetry: TelemetryInfo
    current: ServingSnapshot
    tenants: list[TenantUsage]
    events: list[ClusterEvent]


class RolloutDetail(BaseModel):
    """A rollout plus its audit trail and chain verification."""

    model_config = ConfigDict(frozen=True)

    rollout: Rollout
    audit: list[RolloutEvent]
    audit_verified: bool
    audit_detail: str


class RollbackRequest(BaseModel):
    """Body for a manual rollback."""

    reason: str = Field(min_length=1, description="Why. Recorded in the audit log verbatim.")


class AdapterStatusRequest(BaseModel):
    """Body for an adapter hot-swap."""

    status: str = Field(pattern="^(active|shadow|retired)$")


# --- telemetry -------------------------------------------------------------


@router.get("/telemetry", response_model=TelemetryInfo, summary="Active telemetry source")
async def telemetry_info(telemetry: TelemetryDep, _: RequireRead) -> TelemetryInfo:
    """Report which stream is bound and where its numbers come from."""
    return TelemetryInfo(stream=telemetry.name, provenance=telemetry.provenance)


@router.get("/dashboard", response_model=DashboardSummary, summary="Dashboard first paint")
async def dashboard(telemetry: TelemetryDep, _: RequireRead) -> DashboardSummary:
    """Return the dashboard's initial state in one round trip.

    One call rather than four: the dashboard's first paint is the moment a
    visitor forms an opinion, and four sequential requests on a cold Render
    instance is a visibly slow one.
    """
    current, tenants, events = await asyncio.gather(
        telemetry.snapshot(), telemetry.tenants(), telemetry.events(25)
    )
    return DashboardSummary(
        telemetry=TelemetryInfo(stream=telemetry.name, provenance=telemetry.provenance),
        current=current,
        tenants=list(tenants),
        events=list(events),
    )


@router.get("/telemetry/series", response_model=TimeSeries, summary="One metric over a window")
async def telemetry_series(
    telemetry: TelemetryDep,
    _: RequireRead,
    metric: str = Query(default="output_tokens_per_second"),
    window: WindowQuery = "24h",
) -> TimeSeries:
    """Return a chart-ready series."""
    try:
        return await telemetry.series(metric, window)
    except AttributeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown metric {metric!r}"
        ) from exc


@router.get("/telemetry/events", response_model=list[ClusterEvent], summary="Cluster timeline")
async def telemetry_events(
    telemetry: TelemetryDep, _: RequireRead, limit: int = Query(default=50, ge=1, le=200)
) -> list[ClusterEvent]:
    """Recent scale, spot-reclaim, cold-start and rollout events."""
    return list(await telemetry.events(limit))


@router.get("/telemetry/stream", summary="Live telemetry (SSE)", include_in_schema=False)
async def telemetry_stream(
    request: Request, telemetry: TelemetryDep, _: RequireRead
) -> StreamingResponse:
    """Stream serving snapshots as server-sent events."""

    async def frames() -> AsyncIterator[str]:
        try:
            async for snapshot in telemetry.subscribe(interval_seconds=2.0):
                # Stop promptly when the browser navigates away; otherwise every
                # page change leaks a task holding a 2-second timer forever.
                if await request.is_disconnected():
                    break
                payload = snapshot.model_dump(mode="json")
                yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            raise

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- adapters --------------------------------------------------------------


@router.get("/adapters", response_model=list[Adapter], summary="List adapters")
async def list_adapters(
    store: StoreDep, _: RequireRead, task: str | None = Query(default=None)
) -> list[Adapter]:
    """All served LoRA adapters, highest request share first."""
    return store.list_adapters(task=task)


@router.get("/adapters/{adapter_id}", response_model=Adapter, summary="Adapter detail")
async def get_adapter(adapter_id: str, store: StoreDep, _: RequireRead) -> Adapter:
    """One adapter, including its per-slice quality scores."""
    adapter = store.adapter(adapter_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"No adapter {adapter_id!r}")
    return adapter


@router.post("/adapters/{adapter_id}/status", response_model=Adapter, summary="Hot-swap an adapter")
async def swap_adapter(
    adapter_id: str, body: AdapterStatusRequest, store: StoreDep, tenant: RequireOperate
) -> Adapter:
    """Move an adapter between active, shadow and retired."""
    try:
        return store.set_adapter_status(adapter_id, body.status, actor=tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- rollouts --------------------------------------------------------------


@router.get("/rollouts", response_model=list[Rollout], summary="List rollouts")
async def list_rollouts(controller: ControllerDep, _: RequireRead) -> list[Rollout]:
    """All rollouts, newest first."""
    return controller.list_rollouts()


@router.get("/rollouts/{rollout_id}", response_model=RolloutDetail, summary="Rollout detail")
async def get_rollout(rollout_id: str, controller: ControllerDep, _: RequireRead) -> RolloutDetail:
    """One rollout with its audit trail and a chain verification result."""
    rollout = controller.get(rollout_id)
    if rollout is None:
        raise HTTPException(status_code=404, detail=f"No rollout {rollout_id!r}")

    verified, detail = controller.verify_audit(rollout_id)
    return RolloutDetail(
        rollout=rollout,
        audit=controller.audit(rollout_id),
        audit_verified=verified,
        audit_detail=detail,
    )


@router.post("/rollouts/{rollout_id}/advance", response_model=Rollout, summary="Advance a stage")
async def advance_rollout(
    rollout_id: str, controller: ControllerDep, tenant: RequireOperate
) -> Rollout:
    """Move a rollout one stage forward. Refused while an SLI is breached."""
    try:
        return controller.advance(rollout_id, actor=tenant.id)
    except RolloutTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rollouts/{rollout_id}/rollback", response_model=Rollout, summary="Roll back")
async def rollback_rollout(
    rollout_id: str, body: RollbackRequest, controller: ControllerDep, tenant: RequireOperate
) -> Rollout:
    """Return a rollout to baseline and write a signed audit entry."""
    try:
        return controller.rollback(rollout_id, actor=tenant.id, reason=body.reason)
    except RolloutTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rollouts/{rollout_id}/reconcile", summary="Run one reconciliation pass")
async def reconcile_rollout(
    rollout_id: str, controller: ControllerDep, _: RequireOperate
) -> dict[str, Any]:
    """Evaluate SLIs and auto-roll-back if any are breached."""
    try:
        result = controller.reconcile(rollout_id)
    except RolloutTransitionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "rollout_id": result.rollout_id,
        "stage": result.stage.value,
        "changed": result.changed,
        "reason": result.reason,
        "breached_slis": list(result.breached_slis),
    }


# --- model registry --------------------------------------------------------


@router.get("/registry", response_model=list[ModelVersion], summary="List model versions")
async def list_registry(
    store: StoreDep,
    _: RequireRead,
    family: str | None = Query(default=None),
    model_status: str | None = Query(default=None, alias="status"),
) -> list[ModelVersion]:
    """Every model run, newest first."""
    return store.list_model_versions(family=family, status=model_status)


@router.get("/registry/{version_id}", response_model=ModelVersion, summary="Model version detail")
async def get_registry_entry(version_id: str, store: StoreDep, _: RequireRead) -> ModelVersion:
    """One model version with hyperparameters and promotion history."""
    version = store.model_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No model version {version_id!r}")
    return version


@router.post("/registry/{version_id}/promote", response_model=ModelVersion, summary="Promote")
async def promote_version(version_id: str, store: StoreDep, tenant: RequireOperate) -> ModelVersion:
    """Promote a version, demoting whichever one currently holds the slot."""
    try:
        return store.promote(version_id, actor=tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- evals -----------------------------------------------------------------


@router.get("/evals", response_model=list[EvalReport], summary="List eval reports")
async def list_evals(store: StoreDep, _: RequireRead) -> list[EvalReport]:
    """Pre-computed eval reports, newest first."""
    return store.list_eval_reports()


@router.get("/evals/{version_id}", response_model=EvalReport, summary="Eval report detail")
async def get_eval(version_id: str, store: StoreDep, _: RequireRead) -> EvalReport:
    """One model version's full eval report."""
    report = store.eval_report(version_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"No eval report for {version_id!r}")
    return report
