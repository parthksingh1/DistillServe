"""Telemetry, adapter, rollout, registry and eval contracts.

These are the shapes the dashboards, the rollout controller and the eval
reports all speak. They are defined once here so that `SelfHostedTelemetry`
(reading a live cluster) and `SandboxTelemetry` (reading the reference
dataset) are genuinely drop-in equivalents: neither can drift from the other,
because there is only one definition of what a telemetry frame *is*.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Live telemetry
# ---------------------------------------------------------------------------


class GpuState(StrEnum):
    """Lifecycle state of a GPU node in the serving pool."""

    PROVISIONING = "provisioning"
    WARMING = "warming"
    """Model weights loading and CUDA graphs capturing; not yet serving."""

    SERVING = "serving"
    DRAINING = "draining"
    RECLAIMED = "reclaimed"
    """Spot capacity taken back by the cloud provider."""


class ClusterEventKind(StrEnum):
    """Something that happened to the serving pool."""

    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    SPOT_RECLAIM = "spot_reclaim"
    COLD_START = "cold_start"
    ROLLOUT_ADVANCE = "rollout_advance"
    ROLLOUT_ROLLBACK = "rollout_rollback"
    SLI_BREACH = "sli_breach"


class ClusterEvent(BaseModel):
    """One timeline entry on the GPU pool chart."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ClusterEventKind
    at: dt.datetime
    message: str
    node: str | None = None
    severity: str = Field(default="info", description="info | warning | critical")
    detail: dict[str, str] = Field(default_factory=dict)


class GpuNode(BaseModel):
    """A single accelerator in the serving pool."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: GpuState
    capacity_type: str = Field(description="spot | on-demand")
    utilization: float = Field(ge=0.0, le=1.0)
    kv_cache_utilization: float = Field(ge=0.0, le=1.0)
    memory_used_gb: float = Field(ge=0.0)
    memory_total_gb: float = Field(gt=0.0)
    running_requests: int = Field(ge=0)
    waiting_requests: int = Field(ge=0)


class ServingSnapshot(BaseModel):
    """One instant of serving telemetry.

    This is the frame the Live Dashboard's SSE stream emits. Identical in shape
    whether it came from a real vLLM ``/metrics`` scrape or the reference
    dataset — that equivalence is the design constraint the whole sandbox mode
    is built around.
    """

    model_config = ConfigDict(frozen=True)

    at: dt.datetime
    output_tokens_per_second: float = Field(ge=0.0)
    requests_per_second: float = Field(ge=0.0)
    ttft_p50_ms: float = Field(ge=0.0)
    ttft_p95_ms: float = Field(ge=0.0)
    ttft_p99_ms: float = Field(ge=0.0)
    itl_p50_ms: float = Field(ge=0.0, description="Inter-token latency, median.")
    itl_p95_ms: float = Field(ge=0.0)
    usd_per_million_tokens: float = Field(ge=0.0)
    cache_hit_rate: float = Field(ge=0.0, le=1.0)
    gpu_utilization: float = Field(ge=0.0, le=1.0)
    kv_cache_utilization: float = Field(ge=0.0, le=1.0)
    queue_depth: int = Field(ge=0)
    active_nodes: int = Field(ge=0)
    spot_nodes: int = Field(ge=0)
    acceptance_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="EAGLE-3 draft-token acceptance rate."
    )


class TenantUsage(BaseModel):
    """Per-tenant slice of the dashboard's breakdown table."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    name: str
    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    cache_hit_rate: float = Field(ge=0.0, le=1.0)
    p95_ttft_ms: float = Field(ge=0.0)
    student_share: float = Field(
        ge=0.0, le=1.0, description="Fraction of this tenant's traffic served by the student."
    )


class TimeSeriesPoint(BaseModel):
    """One point on a dashboard chart."""

    model_config = ConfigDict(frozen=True)

    at: dt.datetime
    value: float


class TimeSeries(BaseModel):
    """A named series over a window."""

    model_config = ConfigDict(frozen=True)

    metric: str
    unit: str
    window: str = Field(description="1h | 24h | 7d")
    points: list[TimeSeriesPoint]
    source_id: str = Field(description="Provenance, resolvable in the reference catalog.")


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class SliceScore(BaseModel):
    """Quality on one evaluation slice."""

    model_config = ConfigDict(frozen=True)

    slice: str
    student_score: float = Field(ge=0.0, le=1.0)
    teacher_score: float = Field(ge=0.0, le=1.0)
    sample_count: int = Field(gt=0)

    @property
    def parity(self) -> float:
        """Student score as a fraction of the teacher's.

        Parity, not raw score, is what the rollout gate uses: a slice where the
        teacher itself scores 0.6 should not fail the student for scoring 0.58.
        """
        if self.teacher_score == 0.0:
            return 0.0
        return self.student_score / self.teacher_score


class Adapter(BaseModel):
    """A LoRA adapter served by the platform."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    task: str
    base_model: str
    rank: int = Field(gt=0)
    alpha: int = Field(gt=0)
    quality_score: float = Field(ge=0.0, le=1.0)
    parity: float = Field(ge=0.0, description="Weighted parity against the teacher.")
    request_share: float = Field(ge=0.0, le=1.0)
    requests_24h: int = Field(ge=0)
    trained_at: dt.datetime
    training_examples: int = Field(gt=0)
    status: str = Field(description="active | shadow | retired")
    artifact_uri: str
    slices: list[SliceScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Rollouts
# ---------------------------------------------------------------------------


class RolloutStage(StrEnum):
    """States of the canary state machine.

    Ordered. ``advance`` moves one step right; ``rollback`` jumps directly to
    ``rolled_back`` from anywhere, because a quality incident is not something
    you unwind one stage at a time.
    """

    SHADOW = "shadow"
    CANARY_10 = "canary_10"
    CANARY_50 = "canary_50"
    FULL = "full"
    ROLLED_BACK = "rolled_back"


#: Traffic percentage carried by the candidate at each stage. Shadow is 0:
#: the candidate sees mirrored traffic and its output is scored, but no user
#: ever receives it.
STAGE_TRAFFIC: dict[RolloutStage, int] = {
    RolloutStage.SHADOW: 0,
    RolloutStage.CANARY_10: 10,
    RolloutStage.CANARY_50: 50,
    RolloutStage.FULL: 100,
    RolloutStage.ROLLED_BACK: 0,
}


class SLIStatus(StrEnum):
    """Whether a service-level indicator is within its objective."""

    HEALTHY = "healthy"
    AT_RISK = "at_risk"
    BREACHED = "breached"


class RolloutSLI(BaseModel):
    """One gate the rollout is judged against."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: float
    threshold: float
    unit: str
    comparison: str = Field(description="gte (higher is better) | lte (lower is better)")
    status: SLIStatus

    @property
    def headroom(self) -> float:
        """Signed distance from the threshold, positive when healthy."""
        return (
            self.value - self.threshold if self.comparison == "gte" else self.threshold - self.value
        )


class RolloutEvent(BaseModel):
    """An entry in a rollout's audit trail.

    ``signature`` is an HMAC over the entry's content chained to the previous
    entry's signature. A tampered or removed row breaks the chain, which is
    what makes this an audit log rather than a log.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    rollout_id: str
    at: dt.datetime
    kind: str
    from_stage: RolloutStage | None = None
    to_stage: RolloutStage | None = None
    actor: str
    reason: str
    signature: str
    previous_signature: str | None = None


class Rollout(BaseModel):
    """A candidate model working its way toward full traffic."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    candidate_model: str
    baseline_model: str
    stage: RolloutStage
    traffic_percent: int = Field(ge=0, le=100)
    started_at: dt.datetime
    updated_at: dt.datetime
    auto_rollback: bool = True
    slis: list[RolloutSLI] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


class ModelVersion(BaseModel):
    """A model run, with everything needed to reproduce it."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    family: str = Field(description="teacher | student | quantized | dpo")
    base_model: str
    version: str
    created_at: dt.datetime
    git_sha: str
    dataset_version: str
    seed: int
    hyperparameters: dict[str, str] = Field(default_factory=dict)
    quality_score: float = Field(ge=0.0, le=1.0)
    parity: float = Field(ge=0.0)
    p95_ttft_ms: float = Field(ge=0.0)
    usd_per_million_tokens: float = Field(ge=0.0)
    status: str = Field(description="promoted | candidate | archived")
    promotion_history: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Evals
# ---------------------------------------------------------------------------


class JudgeResult(BaseModel):
    """Pairwise LLM-as-judge outcome for one slice."""

    model_config = ConfigDict(frozen=True)

    slice: str
    student_wins: int = Field(ge=0)
    teacher_wins: int = Field(ge=0)
    ties: int = Field(ge=0)

    @property
    def win_rate(self) -> float:
        """Student win rate counting ties as half, the standard convention."""
        total = self.student_wins + self.teacher_wins + self.ties
        if total == 0:
            return 0.0
        return (self.student_wins + 0.5 * self.ties) / total


class AdversarialResult(BaseModel):
    """One adversarial suite's outcome."""

    model_config = ConfigDict(frozen=True)

    suite: str = Field(description="prompt_injection | jailbreak | distribution_shift")
    attempts: int = Field(gt=0)
    blocked: int = Field(ge=0)
    passed_through: int = Field(ge=0)

    @property
    def block_rate(self) -> float:
        """Fraction of attempts the platform refused."""
        return self.blocked / self.attempts


class ParetoPoint(BaseModel):
    """One model on the latency/cost/quality frontier."""

    model_config = ConfigDict(frozen=True)

    model: str
    quality: float = Field(ge=0.0, le=1.0)
    p95_ttft_ms: float = Field(ge=0.0)
    usd_per_million_tokens: float = Field(ge=0.0)
    on_frontier: bool


class EvalReport(BaseModel):
    """Everything the Eval Reports page shows for one model version."""

    model_config = ConfigDict(frozen=True)

    model_version_id: str
    model_name: str
    generated_at: dt.datetime
    dataset_version: str
    case_count: int = Field(gt=0)
    overall_parity: float = Field(ge=0.0)
    slices: list[SliceScore]
    judge: list[JudgeResult]
    adversarial: list[AdversarialResult]
    pareto: list[ParetoPoint]
    gate_threshold: float = Field(
        ge=0.0, description="Minimum per-slice parity required to pass the CI gate."
    )
    judge_model: str
    judge_drift: float = Field(
        description="Calibration-set agreement drift since the judge was last pinned."
    )

    @property
    def failing_slices(self) -> list[str]:
        """Slices that would fail the CI gate."""
        return [s.slice for s in self.slices if s.parity < self.gate_threshold]
