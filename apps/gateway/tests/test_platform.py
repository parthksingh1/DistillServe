"""Tests for the reference store, rollout controller, registry and platform API."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from distillserve_gateway.app import create_app
from distillserve_gateway.backends.telemetry import SandboxTelemetry
from distillserve_gateway.platform.registry import PlatformStore
from distillserve_gateway.platform.rollouts import (
    AuditLog,
    RolloutController,
    RolloutTransitionError,
)
from distillserve_gateway.reference.store import ReferenceBenchmarkStore, ReferenceDatasetError
from distillserve_schemas import DeploymentMode, RolloutEvent, RolloutStage, SLIStatus

from .conftest import make_settings


@pytest.fixture(scope="module")
def store() -> ReferenceBenchmarkStore:
    return ReferenceBenchmarkStore("data/reference")


def build(tmp_path: Path, **overrides: object) -> FastAPI:
    settings = make_settings(
        DISTILLSERVE_MODE=DeploymentMode.SANDBOX,
        DISTILLSERVE_DB_PATH=str(tmp_path / "platform.sqlite"),
        DISTILLSERVE_AUDIT_DB_PATH=str(tmp_path / "audit.sqlite"),
        **overrides,
    )
    return create_app(settings)


async def call(app: FastAPI, method: str, path: str, **kwargs: object) -> Response:
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            return await http.request(method, path, **kwargs)  # type: ignore[arg-type]


# --- reference store -------------------------------------------------------


def test_the_dataset_reproduces_its_headline_figures(store: ReferenceBenchmarkStore) -> None:
    """The dataset exists to make the README's numbers checkable. This checks them."""
    frames = store.recent_snapshots("7d")
    ttft = sorted(f.ttft_p95_ms for f in frames)
    cost = sorted(f.usd_per_million_tokens for f in frames)
    median = len(frames) // 2

    assert ttft[median] == pytest.approx(210.0, abs=1.0)
    assert cost[median] == pytest.approx(0.19, abs=0.01)
    peak_per_gpu = max(f.output_tokens_per_second / f.active_nodes for f in frames)
    assert peak_per_gpu == pytest.approx(3100, rel=0.05)


def test_a_missing_dataset_fails_loudly() -> None:
    """Empty dashboards would look like an idle cluster, not a broken deployment."""
    with pytest.raises(ReferenceDatasetError, match="does not exist"):
        ReferenceBenchmarkStore("data/does-not-exist")


def test_snapshots_are_deterministic_for_a_given_instant(store: ReferenceBenchmarkStore) -> None:
    """Two viewers side by side must see the same numbers."""
    moment = dt.datetime(2026, 9, 3, 14, 30, tzinfo=dt.UTC)
    assert store.snapshot(moment) == store.snapshot(moment)


def test_the_window_advances_with_the_clock(store: ReferenceBenchmarkStore) -> None:
    early = store.snapshot(dt.datetime(2026, 9, 3, 6, 0, tzinfo=dt.UTC))
    later = store.snapshot(dt.datetime(2026, 9, 3, 18, 0, tzinfo=dt.UTC))
    assert early.requests_per_second != later.requests_per_second


def test_history_is_ordered_oldest_first(store: ReferenceBenchmarkStore) -> None:
    frames = store.recent_snapshots("1h")
    assert frames == sorted(frames, key=lambda f: f.at)


def test_long_windows_are_decimated(store: ReferenceBenchmarkStore) -> None:
    """A browser cannot draw more points than it has pixels."""
    assert len(store.series("ttft_p95_ms", "7d").points) <= 250


def test_the_dataset_has_the_documented_record_counts(store: ReferenceBenchmarkStore) -> None:
    assert len(store.adapters) == 12
    assert len(store.model_versions) == 15
    assert len(store.eval_reports) == 15
    assert len(store.rollouts) == 4


def test_adapter_request_share_sums_to_one(store: ReferenceBenchmarkStore) -> None:
    assert sum(a.request_share for a in store.adapters) == pytest.approx(1.0, abs=0.005)


def test_every_slice_score_carries_a_sample_count(store: ReferenceBenchmarkStore) -> None:
    for report in store.eval_reports:
        assert report.slices
        assert all(s.sample_count > 0 for s in report.slices)


def test_the_promoted_student_matches_the_headline_parity(store: ReferenceBenchmarkStore) -> None:
    promoted = [v for v in store.model_versions if v.status == "promoted" and v.family == "dpo"]
    assert promoted
    assert promoted[0].parity == pytest.approx(0.974, abs=0.001)
    assert promoted[0].usd_per_million_tokens == pytest.approx(0.19, abs=0.001)


def test_events_include_a_spot_reclaim_and_a_rollback(store: ReferenceBenchmarkStore) -> None:
    kinds = {e.kind.value for e in store.events(limit=200)}
    assert "spot_reclaim" in kinds
    assert "rollout_rollback" in kinds
    assert "cold_start" in kinds


# --- telemetry stream ------------------------------------------------------


async def test_sandbox_telemetry_reports_its_provenance(store: ReferenceBenchmarkStore) -> None:
    """A screenshot must be pinnable to an exact dataset revision."""
    telemetry = SandboxTelemetry(store)
    assert telemetry.name == "sandbox"
    assert "Reference dataset" in telemetry.provenance
    assert "H100" in telemetry.provenance


async def test_sandbox_telemetry_streams_frames(store: ReferenceBenchmarkStore) -> None:
    telemetry = SandboxTelemetry(store)
    frames = []
    async for frame in telemetry.subscribe(interval_seconds=0.01):
        frames.append(frame)
        if len(frames) == 3:
            break
    assert len(frames) == 3


# --- audit log -------------------------------------------------------------


def test_audit_entries_chain(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.sqlite", secret="test-secret")
    first = audit.append(rollout_id="r1", kind="advance", actor="op", reason="one")
    second = audit.append(rollout_id="r1", kind="advance", actor="op", reason="two")

    assert first.previous_signature is None
    assert second.previous_signature == first.signature
    assert audit.verify("r1")[0] is True


def test_tampering_breaks_the_chain(tmp_path: Path) -> None:
    """This is the difference between an audit log and a log."""
    import sqlite3

    path = tmp_path / "audit.sqlite"
    audit = AuditLog(path, secret="test-secret")
    audit.append(rollout_id="r1", kind="advance", actor="op", reason="one")
    audit.append(rollout_id="r1", kind="advance", actor="op", reason="two")

    with sqlite3.connect(path) as db:
        db.execute("UPDATE rollout_audit SET reason = 'edited' WHERE reason = 'one'")

    verified, detail = audit.verify("r1")
    assert verified is False
    assert "chain broken" in detail


def test_an_unsigned_log_says_so(tmp_path: Path) -> None:
    """Chained-but-unsigned is a real state and must not be reported as signed."""
    audit = AuditLog(tmp_path / "audit.sqlite")
    audit.append(rollout_id="r1", kind="advance", actor="op", reason="one")

    verified, detail = audit.verify("r1")
    assert verified is True
    assert "unsigned" in detail


# --- rollout controller ----------------------------------------------------


def controller(tmp_path: Path, store: ReferenceBenchmarkStore) -> RolloutController:
    return RolloutController(AuditLog(tmp_path / "a.sqlite", secret="s"), store.rollouts)


def test_advance_moves_exactly_one_stage(tmp_path: Path, store: ReferenceBenchmarkStore) -> None:
    ctrl = controller(tmp_path, store)
    updated = ctrl.advance("rol-2026-09-dpo-003", actor="op")

    assert updated.stage is RolloutStage.CANARY_10
    assert updated.traffic_percent == 10


def test_advance_is_refused_while_an_sli_is_breached(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    """The most expensive mistake this controller could permit."""
    ctrl = controller(tmp_path, store)
    healthy = ctrl.get("rol-2026-09-dpo-003")
    assert healthy is not None

    breached = healthy.model_copy(
        update={"slis": [healthy.slis[0].model_copy(update={"status": SLIStatus.BREACHED})]}
    )
    ctrl._rollouts[breached.id] = breached

    with pytest.raises(RolloutTransitionError, match="breached"):
        ctrl.advance(breached.id, actor="op")


def test_the_seeded_breached_rollout_is_already_rolled_back(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    """The INT4 run is the dataset's worked example of auto-rollback."""
    ctrl = controller(tmp_path, store)
    rollout = ctrl.get("rol-2026-07-int4")

    assert rollout is not None
    assert rollout.stage is RolloutStage.ROLLED_BACK
    assert any(s.status is SLIStatus.BREACHED for s in rollout.slis)


def test_a_rolled_back_rollout_cannot_be_resumed(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    ctrl = controller(tmp_path, store)
    with pytest.raises(RolloutTransitionError, match="rolled back"):
        ctrl.advance("rol-2026-07-int4", actor="op")


def test_advance_stops_at_full(tmp_path: Path, store: ReferenceBenchmarkStore) -> None:
    ctrl = controller(tmp_path, store)
    with pytest.raises(RolloutTransitionError, match="already at full"):
        ctrl.advance("rol-2026-08-fp8-dpo", actor="op")


def test_rollback_jumps_straight_to_rolled_back(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    """A quality incident is not unwound one stage at a time."""
    ctrl = controller(tmp_path, store)
    updated = ctrl.rollback("rol-2026-09-student-006", actor="op", reason="manual test")

    assert updated.stage is RolloutStage.ROLLED_BACK
    assert updated.traffic_percent == 0


def test_rollback_writes_a_signed_audit_entry(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    ctrl = controller(tmp_path, store)
    ctrl.rollback("rol-2026-09-student-006", actor="alice", reason="quality regression")

    entries: list[RolloutEvent] = ctrl.audit("rol-2026-09-student-006")
    assert entries[0].kind == "rollback"
    assert entries[0].actor == "alice"
    assert entries[0].reason == "quality regression"
    assert entries[0].signature
    assert ctrl.verify_audit("rol-2026-09-student-006")[0] is True


def test_reconcile_auto_rolls_back_a_breached_rollout(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    ctrl = controller(tmp_path, store)
    healthy = ctrl.reconcile("rol-2026-09-dpo-003")
    assert healthy.changed is False

    # Force a breach on a healthy rollout, then reconcile it.
    rollout = ctrl.get("rol-2026-09-student-006")
    assert rollout is not None
    breached = rollout.model_copy(
        update={"slis": [s.model_copy(update={"status": SLIStatus.BREACHED}) for s in rollout.slis]}
    )
    ctrl._rollouts[breached.id] = breached

    result = ctrl.reconcile(breached.id)
    assert result.changed is True
    assert result.stage is RolloutStage.ROLLED_BACK
    assert result.breached_slis


# --- platform store --------------------------------------------------------


def test_seeding_is_idempotent(tmp_path: Path, store: ReferenceBenchmarkStore) -> None:
    """Re-seeding on boot would silently discard a promotion made in the UI."""
    db = PlatformStore(tmp_path / "p.sqlite")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )
    db.promote("mv-student-006", actor="op")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )

    promoted = db.model_version("mv-student-006")
    assert promoted is not None
    assert promoted.status == "promoted"


def test_promotion_is_exclusive_within_a_family(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    """Two promoted students is an ambiguous routing target."""
    db = PlatformStore(tmp_path / "p.sqlite")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )
    db.promote("mv-student-006", actor="op")

    promoted = [v for v in db.list_model_versions(family="student") if v.status == "promoted"]
    assert [v.id for v in promoted] == ["mv-student-006"]


def test_promotion_is_recorded_in_history(tmp_path: Path, store: ReferenceBenchmarkStore) -> None:
    db = PlatformStore(tmp_path / "p.sqlite")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )
    version = db.promote("mv-student-006", actor="alice")
    assert any("alice" in entry for entry in version.promotion_history)


def test_adapter_hot_swap_persists(tmp_path: Path, store: ReferenceBenchmarkStore) -> None:
    db = PlatformStore(tmp_path / "p.sqlite")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )
    db.set_adapter_status("adp-code-review", "retired", actor="op")

    reopened = PlatformStore(tmp_path / "p.sqlite")
    adapter = reopened.adapter("adp-code-review")
    assert adapter is not None
    assert adapter.status == "retired"


def test_an_unknown_adapter_status_is_rejected(
    tmp_path: Path, store: ReferenceBenchmarkStore
) -> None:
    db = PlatformStore(tmp_path / "p.sqlite")
    db.seed(
        model_versions=store.model_versions,
        adapters=store.adapters,
        eval_reports=store.eval_reports,
    )
    with pytest.raises(ValueError, match="active, shadow or retired"):
        db.set_adapter_status("adp-code-review", "deleted", actor="op")


# --- HTTP surface ----------------------------------------------------------


async def test_dashboard_returns_first_paint_in_one_call(tmp_path: Path) -> None:
    response = await call(build(tmp_path), "GET", "/v1/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["telemetry"]["stream"] == "sandbox"
    assert body["current"]["output_tokens_per_second"] > 0
    assert body["tenants"]
    assert body["events"]


async def test_telemetry_series_is_chart_ready(tmp_path: Path) -> None:
    response = await call(
        build(tmp_path), "GET", "/v1/telemetry/series", params={"metric": "ttft_p95_ms"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["unit"] == "ms"
    assert len(body["points"]) > 10


async def test_adapters_and_registry_are_served(tmp_path: Path) -> None:
    app = build(tmp_path)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            adapters = await http.get("/v1/adapters")
            registry = await http.get("/v1/registry")
            evals = await http.get("/v1/evals")

    assert len(adapters.json()) == 12
    assert len(registry.json()) == 15
    assert len(evals.json()) == 15


async def test_rollback_over_http_writes_an_audit_entry(tmp_path: Path) -> None:
    app = build(tmp_path)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            rolled = await http.post(
                "/v1/rollouts/rol-2026-09-student-006/rollback",
                json={"reason": "manual rollback from the console"},
            )
            detail = await http.get("/v1/rollouts/rol-2026-09-student-006")

    assert rolled.status_code == 200
    assert rolled.json()["stage"] == "rolled_back"

    body = detail.json()
    assert body["audit_verified"] is True
    assert body["audit"][0]["kind"] == "rollback"
    assert body["audit"][0]["signature"]


async def test_advancing_a_breached_rollout_is_a_conflict(tmp_path: Path) -> None:
    response = await call(build(tmp_path), "POST", "/v1/rollouts/rol-2026-07-int4/advance")
    assert response.status_code == 409


async def test_a_demo_tenant_cannot_advance_a_rollout(tmp_path: Path) -> None:
    """The whole point of the demo scope: drive everything, change nothing."""
    app = build(tmp_path, DISTILLSERVE_AUTH_REQUIRED=True, DISTILLSERVE_DEMO_TOKEN="demo-tok")
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            headers = {"Authorization": "Bearer demo-tok"}
            readable = await http.get("/v1/adapters", headers=headers)
            forbidden = await http.post("/v1/rollouts/rol-2026-09-dpo-003/advance", headers=headers)

    assert readable.status_code == 200
    assert forbidden.status_code == 403


async def test_unknown_ids_are_404(tmp_path: Path) -> None:
    app = build(tmp_path)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            assert (await http.get("/v1/adapters/nope")).status_code == 404
            assert (await http.get("/v1/registry/nope")).status_code == 404
            assert (await http.get("/v1/evals/nope")).status_code == 404
