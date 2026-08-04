"""Platform-level contracts: deployment mode, build identity, health probes.

These are the first schemas defined because every later phase depends on them:
the mode selects which ``InferenceBackend`` and ``TelemetryStream`` are bound at
startup, and the build identity is what lets a screenshot, a trace and a
deployed commit be tied back together.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DeploymentMode(StrEnum):
    """How this DistillServe instance sources inference and telemetry.

    The three modes are peers, not a ladder — they differ only in which
    ``InferenceBackend`` / ``TelemetryStream`` pair is bound at startup. Every
    other code path (routing, cache, guardrails, cost, evals, rollouts) is
    shared verbatim.

    Attributes:
        HOSTED: Route to hosted providers through LiteLLM. Needs only API keys.
        SELF_HOSTED: Route to a vLLM OpenAI-compatible endpoint on a GPU
            cluster provisioned from ``infra/k8s/``.
        SANDBOX: Generation is served by the hosted backend while
            ``self_hosted`` telemetry is streamed from the reference benchmark
            dataset in ``data/reference/``.
    """

    HOSTED = "hosted"
    SELF_HOSTED = "self_hosted"
    SANDBOX = "sandbox"


class RuntimeEnvironment(StrEnum):
    """Deployment environment, used for log routing and trace attributes."""

    LOCAL = "local"
    PREVIEW = "preview"
    PRODUCTION = "production"
    TEST = "test"


class HealthStatus(StrEnum):
    """Outcome of a health or readiness probe."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class ServiceIdentity(BaseModel):
    """Who this process is and which commit it was built from.

    ``git_sha`` is resolved at import time from the platform-provided build env
    var, falling back to the working tree. It is echoed on every probe so a
    dashboard number can always be traced to the exact code that produced it.
    """

    model_config = ConfigDict(frozen=True)

    service: str = Field(description="Service name, e.g. 'gateway'.")
    version: str = Field(description="Semantic version of the service package.")
    git_sha: str = Field(description="Full commit SHA, or 'unknown' outside a checkout.")
    git_sha_short: str = Field(description="First 12 characters of `git_sha`.")
    mode: DeploymentMode = Field(description="Active deployment mode.")
    environment: RuntimeEnvironment = Field(description="Deployment environment.")


class LivenessResponse(BaseModel):
    """Body returned by ``GET /healthz``.

    Liveness answers "is this process healthy enough to keep running", so it
    deliberately touches no dependency: a Redis outage must not cause an
    orchestrator to restart-loop a gateway that is otherwise fine.
    """

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    identity: ServiceIdentity
    uptime_seconds: float = Field(ge=0.0, description="Seconds since app startup.")


class ReadinessCheck(BaseModel):
    """Result of one dependency probe contributing to readiness."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Dependency identifier, e.g. 'redis'.")
    status: HealthStatus
    required: bool = Field(
        description="Whether a failure of this check makes the service unready. "
        "Optional dependencies degrade the service instead of failing it."
    )
    detail: str | None = Field(default=None, description="Human-readable diagnostic.")
    latency_ms: float | None = Field(default=None, ge=0.0, description="Probe duration.")


class ReadinessResponse(BaseModel):
    """Body returned by ``GET /readyz``.

    Readiness answers "should this process receive traffic", so it *does* touch
    dependencies. The aggregate is ``failed`` if any required check failed,
    ``degraded`` if only optional checks failed, otherwise ``ok``.
    """

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    identity: ServiceIdentity
    checks: list[ReadinessCheck]
