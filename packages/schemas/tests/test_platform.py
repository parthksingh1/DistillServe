"""Tests pinning the wire contracts the frontend and gateway both depend on."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from distillserve_schemas import (
    DeploymentMode,
    HealthStatus,
    LivenessResponse,
    ReadinessCheck,
    RuntimeEnvironment,
    ServiceIdentity,
)


def test_deployment_mode_wire_values_are_stable() -> None:
    """These strings appear in env vars, URLs and the UI mode switch; they are API."""
    assert [mode.value for mode in DeploymentMode] == ["hosted", "self_hosted", "sandbox"]


def test_health_status_wire_values_are_stable() -> None:
    assert [status.value for status in HealthStatus] == ["ok", "degraded", "failed"]


def _identity() -> ServiceIdentity:
    return ServiceIdentity(
        service="distillserve-gateway",
        version="0.1.0",
        git_sha="0" * 40,
        git_sha_short="0" * 12,
        mode=DeploymentMode.SANDBOX,
        environment=RuntimeEnvironment.LOCAL,
    )


def test_identity_is_frozen() -> None:
    identity = _identity()
    with pytest.raises(ValidationError):
        identity.service = "other"


def test_liveness_rejects_negative_uptime() -> None:
    with pytest.raises(ValidationError):
        LivenessResponse(status=HealthStatus.OK, identity=_identity(), uptime_seconds=-1.0)


def test_readiness_check_defaults_are_optional_fields_only() -> None:
    check = ReadinessCheck(name="redis", status=HealthStatus.OK, required=True)
    assert check.detail is None
    assert check.latency_ms is None
