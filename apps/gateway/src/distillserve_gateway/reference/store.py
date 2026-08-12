"""``ReferenceBenchmarkStore`` — the reference dataset, loaded and served.

Backs ``DISTILLSERVE_MODE=sandbox``. Everything it returns comes from
``data/reference/``, which is generated from ``generator.yaml`` and content
hashed into ``manifest.json``, so a figure on a dashboard can always be traced
back to a dataset revision and from there to a cited source.

The store's one interesting behaviour is how it makes a fixed dataset look
live. Frames carry an offset from a fixed anchor rather than an absolute time.
At read time the store maps *now* onto that 7-day window and shifts timestamps
forward. The consequence is exactly what the sandbox needs:

* the dashboard is always moving, because the position in the window advances
  with the wall clock;
* it tells the *same story* on every visit, because position is a pure
  function of the clock and the seed — a demo at 3pm on Tuesday and one at 9am
  on Friday both show a coherent day of traffic;
* nothing is random at read time, so two browsers open side by side agree.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import cached_property
from pathlib import Path
from typing import Any

from distillserve_otel import get_logger
from distillserve_schemas import (
    Adapter,
    ClusterEvent,
    EvalReport,
    ModelVersion,
    Rollout,
    ServingSnapshot,
    TenantUsage,
    TimeSeries,
    TimeSeriesPoint,
)

log = get_logger(__name__)

#: Windows the dashboard offers, and how many minutes of trace each covers.
WINDOW_MINUTES: dict[str, int] = {"1h": 60, "24h": 24 * 60, "7d": 7 * 24 * 60}

#: Upper bound on points in a chart series. A browser cannot draw more than
#: it has pixels for, and beyond this the payload dominates page load time.
_MAX_CHART_POINTS = 240


class ReferenceDatasetError(RuntimeError):
    """The reference dataset is missing or unreadable.

    Raised loudly rather than degrading to empty dashboards: in sandbox mode
    this dataset *is* the telemetry, and silently serving zeros would look like
    an idle cluster rather than a broken deployment.
    """


class ReferenceBenchmarkStore:
    """Reads the reference dataset and projects it onto the wall clock."""

    def __init__(self, data_dir: Path | str = "data/reference", *, seed: int = 20250901) -> None:
        """Bind to a dataset directory.

        Args:
            data_dir: Directory holding the generated payloads.
            seed: Recorded for provenance and used to offset the wall-clock
                projection, so two deployments with different seeds tell
                different-but-stable stories.
        """
        self._dir = Path(data_dir)
        self._seed = seed
        if not self._dir.is_dir():
            raise ReferenceDatasetError(
                f"Reference dataset directory {self._dir} does not exist. "
                "Run `make reference-refresh` to generate it."
            )

    # -- loading ------------------------------------------------------------

    def _read(self, name: str) -> dict[str, Any]:
        """Load one payload file."""
        path = self._dir / name
        if not path.is_file():
            raise ReferenceDatasetError(
                f"Reference payload {path} is missing. Run `make reference-refresh`."
            )
        return dict(json.loads(path.read_text(encoding="utf-8")))

    @cached_property
    def manifest(self) -> dict[str, Any]:
        """The dataset manifest, including its content digest and sources."""
        return self._read("manifest.json")

    @cached_property
    def dataset_digest(self) -> str:
        """Content hash of the dataset, reported alongside telemetry."""
        return str(self.manifest.get("dataset_digest", "unknown"))

    @cached_property
    def reference_deployment(self) -> dict[str, Any]:
        """The cluster shape this telemetry represents."""
        return dict(self.manifest.get("reference_deployment", {}))

    @cached_property
    def _frames(self) -> list[dict[str, Any]]:
        return list(self._read("serving_trace.json")["frames"])

    @cached_property
    def _events(self) -> list[dict[str, Any]]:
        return list(self._read("cluster_events.json")["events"])

    @cached_property
    def _step_minutes(self) -> int:
        return int(self._read("serving_trace.json")["step_minutes"])

    # -- wall-clock projection ---------------------------------------------

    def _window_minutes(self) -> int:
        """Total minutes of trace available."""
        return len(self._frames) * self._step_minutes

    def _position(self, now: dt.datetime) -> int:
        """Map ``now`` onto a frame index.

        Modulo the trace length, so the window loops seamlessly, offset by the
        seed so different deployments sit at different points in the cycle.
        """
        minutes = int(now.timestamp() // 60) + self._seed
        return (minutes // self._step_minutes) % len(self._frames)

    def _shift(self, frame: dict[str, Any], now: dt.datetime, index_delta: int) -> dt.datetime:
        """Return the wall-clock time a frame should claim."""
        del frame
        return now - dt.timedelta(minutes=index_delta * self._step_minutes)

    # -- public reads -------------------------------------------------------

    def snapshot(self, now: dt.datetime | None = None) -> ServingSnapshot:
        """Return the current serving telemetry frame."""
        moment = now or dt.datetime.now(dt.UTC)
        frame = self._frames[self._position(moment)]
        return ServingSnapshot.model_validate({**frame, "at": moment})

    def recent_snapshots(
        self, window: str = "1h", now: dt.datetime | None = None
    ) -> list[ServingSnapshot]:
        """Return the frames covering ``window``, oldest first."""
        if window not in WINDOW_MINUTES:
            raise ValueError(f"window must be one of {sorted(WINDOW_MINUTES)}, got {window!r}")

        moment = now or dt.datetime.now(dt.UTC)
        count = min(len(self._frames), WINDOW_MINUTES[window] // self._step_minutes)
        end = self._position(moment)

        out: list[ServingSnapshot] = []
        for step in range(count - 1, -1, -1):
            frame = self._frames[(end - step) % len(self._frames)]
            out.append(
                ServingSnapshot.model_validate({**frame, "at": self._shift(frame, moment, step)})
            )
        return out

    def series(
        self, metric: str, window: str = "24h", now: dt.datetime | None = None
    ) -> TimeSeries:
        """Return one metric as a chart-ready series.

        Long windows are decimated so a 7-day chart ships hundreds of points
        rather than two thousand: the browser cannot draw more than it has
        pixels for, and the payload dominates the page's load time.
        """
        snapshots = self.recent_snapshots(window, now)
        # Ceiling division, so the cap is a guarantee rather than an
        # approximation: floor division overshoots whenever the length is
        # not a clean multiple of the target.
        stride = max(1, -(-len(snapshots) // _MAX_CHART_POINTS))
        points = [
            TimeSeriesPoint(at=s.at, value=float(getattr(s, metric))) for s in snapshots[::stride]
        ]
        return TimeSeries(
            metric=metric,
            unit=_UNITS.get(metric, ""),
            window=window,
            points=points,
            source_id="distillserve-canonical-run",
        )

    def events(self, limit: int = 50, now: dt.datetime | None = None) -> list[ClusterEvent]:
        """Return recent cluster and rollout events, newest first."""
        moment = now or dt.datetime.now(dt.UTC)
        end_minutes = self._position(moment) * self._step_minutes

        shifted: list[ClusterEvent] = []
        for event in self._events:
            delta = end_minutes - int(event["offset_minutes"])
            if delta < 0:
                delta += self._window_minutes()
            shifted.append(
                ClusterEvent.model_validate({**event, "at": moment - dt.timedelta(minutes=delta)})
            )
        shifted.sort(key=lambda e: e.at, reverse=True)
        return shifted[:limit]

    @cached_property
    def adapters(self) -> list[Adapter]:
        """The twelve adapter records."""
        return [Adapter.model_validate(a) for a in self._read("adapters.json")["adapters"]]

    @cached_property
    def model_versions(self) -> list[ModelVersion]:
        """The fifteen model-registry entries."""
        return [
            ModelVersion.model_validate(v) for v in self._read("model_registry.json")["versions"]
        ]

    @cached_property
    def rollouts(self) -> list[Rollout]:
        """Rollout records with their SLIs."""
        return [Rollout.model_validate(r) for r in self._read("rollouts.json")["rollouts"]]

    @cached_property
    def eval_reports(self) -> list[EvalReport]:
        """Pre-computed eval reports, one per model version."""
        return [EvalReport.model_validate(r) for r in self._read("eval_reports.json")["reports"]]

    @cached_property
    def tenants(self) -> list[TenantUsage]:
        """Per-tenant usage breakdown."""
        return [TenantUsage.model_validate(t) for t in self._read("tenants.json")["tenants"]]

    def adapter(self, adapter_id: str) -> Adapter | None:
        """Look up one adapter."""
        return next((a for a in self.adapters if a.id == adapter_id), None)

    def model_version(self, version_id: str) -> ModelVersion | None:
        """Look up one model version."""
        return next((v for v in self.model_versions if v.id == version_id), None)

    def eval_report(self, version_id: str) -> EvalReport | None:
        """Look up one eval report."""
        return next((r for r in self.eval_reports if r.model_version_id == version_id), None)


#: Units for the metrics the dashboard charts. Kept next to the store so a new
#: metric cannot reach a chart without an axis label.
_UNITS: dict[str, str] = {
    "output_tokens_per_second": "tok/s",
    "requests_per_second": "req/s",
    "ttft_p50_ms": "ms",
    "ttft_p95_ms": "ms",
    "ttft_p99_ms": "ms",
    "itl_p50_ms": "ms",
    "itl_p95_ms": "ms",
    "usd_per_million_tokens": "$/M tok",
    "cache_hit_rate": "ratio",
    "gpu_utilization": "ratio",
    "kv_cache_utilization": "ratio",
    "queue_depth": "requests",
    "acceptance_rate": "ratio",
}
