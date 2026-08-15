"""Contract tests for the operational endpoints."""

from __future__ import annotations

import asyncio

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient

from distillserve_gateway.core import readiness
from distillserve_gateway.core.build_info import git_sha
from distillserve_gateway.core.settings import Settings
from distillserve_schemas import DeploymentMode, HealthStatus, ReadinessCheck

from .conftest import build_client, make_settings


async def test_healthz_reports_identity(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == HealthStatus.OK
    assert body["identity"]["service"] == "distillserve-gateway"
    assert body["identity"]["mode"] == DeploymentMode.HOSTED
    assert body["identity"]["environment"] == "test"
    assert body["identity"]["git_sha"] == git_sha()
    assert body["identity"]["git_sha_short"] == git_sha()[:12]
    assert body["uptime_seconds"] >= 0.0


async def test_readyz_ok_in_hosted_mode_with_provider_key(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200

    body = response.json()
    # Langfuse is unconfigured in tests, which degrades the service but,
    # because trace export is optional, must never make it unready.
    assert body["status"] == HealthStatus.DEGRADED
    names = {check["name"] for check in body["checks"]}
    assert names == {
        "provider_credentials",
        "vllm_endpoint",
        "reference_dataset",
        "langfuse_export",
    }


async def test_readyz_fails_without_provider_credentials() -> None:
    app, http = build_client(make_settings(GROQ_API_KEY=None))
    async with LifespanManager(app), http:  # type: ignore[arg-type]
        response = await http.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == HealthStatus.FAILED
    failed = [c for c in body["checks"] if c["status"] == HealthStatus.FAILED]
    assert [c["name"] for c in failed] == ["provider_credentials"]


async def test_metrics_exposes_build_info(client: AsyncClient) -> None:
    # Touch a route first so the request counter has at least one sample.
    await client.get("/healthz")
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "distillserve_build_info" in response.text
    assert "distillserve_http_requests_total" in response.text


async def _run_probes(settings: Settings) -> list[ReadinessCheck]:
    return list(await asyncio.gather(*(probe(settings) for probe in readiness.DEFAULT_PROBES)))


@pytest.mark.parametrize(
    ("mode", "expected_required", "extra"),
    [
        # Sandbox generates through hosted providers *and* serves telemetry from
        # the reference dataset, so both are load-bearing for it.
        (DeploymentMode.SANDBOX, {"provider_credentials", "reference_dataset"}, {}),
        (
            DeploymentMode.SELF_HOSTED,
            {"vllm_endpoint"},
            {"VLLM_ENDPOINT": "http://vllm.internal:8000/v1"},
        ),
        (DeploymentMode.HOSTED, {"provider_credentials"}, {}),
    ],
)
async def test_mode_selects_its_required_probes(
    mode: DeploymentMode, expected_required: set[str], extra: dict[str, object]
) -> None:
    """Each mode marks exactly the dependencies it cannot serve without."""
    settings = make_settings(DISTILLSERVE_MODE=mode, **extra)
    checks = await _run_probes(settings)

    required = {check.name for check in checks if check.required}
    assert required == expected_required


async def test_all_probes_pass_in_sandbox_mode() -> None:
    """Sandbox must be ready out of the box — that is the point of the mode."""
    settings = make_settings(DISTILLSERVE_MODE=DeploymentMode.SANDBOX)
    checks = await _run_probes(settings)

    failures = [c for c in checks if c.required and c.status is not HealthStatus.OK]
    assert failures == []
