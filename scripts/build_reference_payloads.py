"""Build the reference dataset payloads from ``data/reference/generator.yaml``.

Called by ``make reference-refresh`` (via ``refresh_reference_dataset.py``).

The dataset is *generated*, not hand-written, for one reason: the numbers are
interdependent. Throughput determines cost per token; utilisation determines
how much of that cost is idle; node count follows from queue depth. Editing a
JSON file by hand would let those drift apart, and a dashboard whose numbers
contradict each other is worse than no dashboard.

Everything is derived from the ``headline`` block, which holds each measured
figure exactly once. Generation is deterministic: same seed and same
generator.yaml produce byte-identical payloads, which is what lets CI check
the manifest for drift.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "reference"
GENERATOR = DATA_DIR / "generator.yaml"

#: Fixed epoch for generation. Payload timestamps are *relative offsets* from
#: this anchor; the store shifts them onto the wall clock at read time. That is
#: what makes the dashboard show "live" telemetry that is nonetheless identical
#: on every visit.
ANCHOR = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.UTC)

SEED = 20250901


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def _load() -> dict[str, Any]:
    return dict(yaml.safe_load(GENERATOR.read_text(encoding="utf-8")))


def _write(name: str, payload: dict[str, Any]) -> Path:
    path = DATA_DIR / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _queue_pressure(utilisation: float) -> float:
    """Queueing pressure at a given utilisation.

    M/M/1-shaped: 1/(1-rho) blows up as the pool saturates, which is what makes
    TTFT degrade superlinearly at the top of the load curve rather than
    linearly.

    Two details matter. The 0.94 factor accounts for the pool never being a
    single perfectly-shared queue, so pressure at 100% utilisation is large but
    finite. And the clamp is deliberately loose (12, not 3): a tight clamp
    pinned the value across almost the whole operating range and flattened the
    derived latency curve into a straight line.
    """
    return min(12.0, 1.0 / max(0.06, 1.0 - 0.94 * utilisation))


def build_serving_trace(config: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Emit a 7-day, 5-minute-resolution serving trace.

    Load follows a daily sinusoid; every other metric is *derived* from the
    instantaneous load rather than generated independently:

    * queue depth rises superlinearly as utilisation approaches 1.0, which is
      what queueing theory gives and what makes TTFT degrade at the top end;
    * TTFT is the p95 headline scaled by that queueing pressure;
    * cost per token falls as throughput rises, since the pool is billed by
      the second either way.

    Deriving rather than fabricating is the point: a viewer can check that the
    curves are consistent with each other, because they are.
    """
    head = config["headline"]
    work = config["workload"]
    cluster = config["cluster"]

    step_minutes = 5
    points = 7 * 24 * 60 // step_minutes
    gpu_count = cluster["gpu_count"]
    peak_capacity_rps = head["tokens_per_second_per_gpu"] * gpu_count / work["mean_output_tokens"]

    # The headline figures are the *steady state* of this workload, so the
    # generator is calibrated against mean load rather than against an
    # arbitrary anchor. Without this the trace drifts away from the numbers it
    # is supposed to reproduce, and the dashboard stops agreeing with the
    # README — which is exactly the failure mode a generated dataset exists to
    # prevent.
    mean_load = min(0.98, work["requests_per_second_mean"] / peak_capacity_rps)
    mean_nodes = min(
        cluster["max_nodes"],
        max(cluster["min_nodes"], math.ceil(mean_load * cluster["max_nodes"] + 0.35)),
    )
    mean_node_load = min(0.99, mean_load * cluster["max_nodes"] / mean_nodes)
    reference_pressure = _queue_pressure(mean_node_load)

    # Pass 1: the load curve and everything derived from it, unnormalised.
    samples: list[dict[str, float]] = []
    for index in range(points):
        minutes = index * step_minutes
        at = ANCHOR + dt.timedelta(minutes=minutes)
        phase = 2 * math.pi * (minutes / 60.0) / work["period_hours"]

        rps = work["requests_per_second_mean"] + work["requests_per_second_amplitude"] * math.sin(
            phase
        )
        rps *= 1.0 + rng.uniform(-work["noise_fraction"], work["noise_fraction"])
        rps = max(1.0, rps)

        load = min(0.98, rps / peak_capacity_rps)
        # Nodes track load with a floor and ceiling, exactly as the Karpenter
        # provisioner is configured to.
        nodes = min(
            cluster["max_nodes"],
            max(cluster["min_nodes"], math.ceil(load * cluster["max_nodes"] + 0.35)),
        )
        node_load = min(0.99, load * cluster["max_nodes"] / nodes)

        pressure = _queue_pressure(node_load)
        # Latency tracks queueing pressure, damped so a momentary spike does
        # not swing TTFT by more than the load actually justifies.
        latency_scale = 0.55 + 0.45 * (pressure / reference_pressure)

        samples.append(
            {
                "minutes": float(minutes),
                "rps": rps,
                "node_load": node_load,
                "nodes": float(nodes),
                "pressure": pressure,
                "latency_scale": latency_scale,
                "load": load,
            }
        )

    # Pass 2: rescale so the *median* frame reproduces the headline figures
    # exactly. The headlines are medians of a real trace, and a sinusoidal load
    # with quantised node counts does not put its median at its mean — so
    # normalising against the mean alone leaves the median several ms off, and
    # the dataset stops matching the numbers it exists to reproduce.
    median_scale = sorted(sample["latency_scale"] for sample in samples)[len(samples) // 2]
    median_util = sorted(sample["node_load"] for sample in samples)[len(samples) // 2]

    frames: list[dict[str, Any]] = []
    for sample in samples:
        minutes = int(sample["minutes"])
        at = ANCHOR + dt.timedelta(minutes=minutes)
        node_load = sample["node_load"]
        nodes = int(sample["nodes"])
        pressure = sample["pressure"]
        load = sample["load"]

        latency = sample["latency_scale"] / median_scale
        ttft_p95 = head["p95_ttft_ms"] * latency
        ttft_p50 = head["p50_ttft_ms"] * latency
        ttft_p99 = head["p99_ttft_ms"] * latency
        tok_s = head["tokens_per_second_per_gpu"] * nodes * node_load
        cost = head["usd_per_million_tokens"] * (median_util / max(0.15, node_load))

        frames.append(
            {
                "offset_minutes": minutes,
                "at": _iso(at),
                "output_tokens_per_second": round(tok_s, 1),
                "requests_per_second": round(sample["rps"], 2),
                "ttft_p50_ms": round(ttft_p50, 1),
                "ttft_p95_ms": round(ttft_p95, 1),
                "ttft_p99_ms": round(ttft_p99, 1),
                "itl_p50_ms": round(head["p50_itl_ms"] * (0.9 + 0.2 * node_load), 2),
                "itl_p95_ms": round(head["p95_itl_ms"] * (0.9 + 0.25 * node_load), 2),
                "usd_per_million_tokens": round(cost, 4),
                "cache_hit_rate": round(min(0.62, head["cache_hit_rate"] * (0.85 + 0.4 * load)), 4),
                "gpu_utilization": round(node_load, 4),
                "kv_cache_utilization": round(min(0.97, node_load * 0.92 + 0.05), 4),
                "queue_depth": int(max(0, (pressure - 1.0) * 24)),
                "active_nodes": nodes,
                "spot_nodes": round(nodes * head["spot_fraction"]),
                "acceptance_rate": round(head["acceptance_rate"] * (1.02 - 0.06 * node_load), 4),
            }
        )

    return {
        "source_id": config["source_id"],
        "description": (
            "7-day serving trace at 5-minute resolution for the 8xH100 reference deployment."
        ),
        "anchor": _iso(ANCHOR),
        "step_minutes": step_minutes,
        "frames": frames,
    }


def build_cluster_events(config: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Emit the GPU pool timeline: scale events, spot reclaims, cold starts.

    Spot reclaims are placed deliberately, not sprinkled randomly: each one is
    followed by a replacement scale-up and a cold start, because that is the
    sequence an operator actually sees and the reason cold-start time matters.
    """
    cluster = config["cluster"]
    events: list[dict[str, Any]] = []
    counter = 0

    def add(offset_minutes: int, kind: str, message: str, **detail: str) -> None:
        nonlocal counter
        counter += 1
        events.append(
            {
                "id": f"evt-{counter:04d}",
                "kind": kind,
                "offset_minutes": offset_minutes,
                "at": _iso(ANCHOR + dt.timedelta(minutes=offset_minutes)),
                "message": message,
                "node": detail.pop("node", None),
                "severity": detail.pop("severity", "info"),
                "detail": detail,
            }
        )

    # Daily scale-up into the peak and scale-down out of it.
    for day in range(7):
        base = day * 24 * 60
        add(
            base + 7 * 60,
            "scale_up",
            "Queue depth crossed 12 for 60s; provisioning 2 nodes",
            trigger="queue_depth",
            node=f"h100-spot-{day}a",
        )
        add(
            base + 7 * 60 + 2,
            "cold_start",
            f"Node warm in {cluster['cold_start_seconds']}s (weights + CUDA graphs)",
            node=f"h100-spot-{day}a",
            seconds=str(cluster["cold_start_seconds"]),
        )
        add(
            base + 21 * 60,
            "scale_down",
            "p95 TTFT 84ms below objective for 10m; draining 2 nodes",
            trigger="ttft_headroom",
            node=f"h100-spot-{day}a",
        )

    # Spot reclaims, each followed by replacement capacity.
    for day, minute in ((1, 14 * 60 + 22), (3, 9 * 60 + 47), (5, 17 * 60 + 8)):
        offset = day * 24 * 60 + minute
        node = f"h100-spot-{day}b"
        add(
            offset,
            "spot_reclaim",
            "Spot reclamation notice received; draining with 120s budget",
            node=node,
            severity="warning",
            notice_seconds="120",
        )
        add(
            offset + 1,
            "scale_up",
            "Replacing reclaimed capacity with on-demand node",
            node=f"h100-od-{day}b",
            capacity_type="on-demand",
        )
        add(
            offset + 3,
            "cold_start",
            f"Replacement node serving after {cluster['cold_start_seconds']}s",
            node=f"h100-od-{day}b",
        )

    # Rollout timeline, including the one that rolled back.
    add(
        2 * 24 * 60 + 10 * 60,
        "rollout_advance",
        "rol-2026-08-fp8-dpo advanced shadow -> canary_10",
        rollout="rol-2026-08-fp8-dpo",
    )
    add(
        3 * 24 * 60 + 11 * 60,
        "rollout_advance",
        "rol-2026-08-fp8-dpo advanced canary_10 -> canary_50",
        rollout="rol-2026-08-fp8-dpo",
    )
    add(
        4 * 24 * 60 + 9 * 60,
        "rollout_advance",
        "rol-2026-08-fp8-dpo advanced canary_50 -> full",
        rollout="rol-2026-08-fp8-dpo",
    )
    add(
        1 * 24 * 60 + 16 * 60,
        "sli_breach",
        "rol-2026-07-int4 reasoning-slice parity 0.891 below 0.95 gate",
        rollout="rol-2026-07-int4",
        severity="critical",
        sli="quality_parity",
    )
    add(
        1 * 24 * 60 + 16 * 60 + 1,
        "rollout_rollback",
        "rol-2026-07-int4 auto-rolled back to baseline; 0 requests affected",
        rollout="rol-2026-07-int4",
        severity="critical",
    )

    events.sort(key=lambda e: e["offset_minutes"])
    del rng  # events are fully specified; no randomness by design
    return {
        "source_id": config["source_id"],
        "description": "GPU pool and rollout timeline for the reference deployment.",
        "anchor": _iso(ANCHOR),
        "events": events,
    }


def build_adapters(config: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Emit the twelve adapter records with per-slice quality scores."""
    slices = {s["name"]: s for s in config["slices"]}
    total_requests_24h = 3_100_000

    records: list[dict[str, Any]] = []
    for index, adapter in enumerate(config["adapters"]):
        task = adapter["task"]
        teacher = slices[task]["teacher"]
        # Per-slice scores fan out around the adapter's headline quality: an
        # adapter is strongest on its own task and degrades on neighbouring
        # ones, which is exactly what the heatmap should show.
        slice_scores = []
        for name, spec in slices.items():
            if name == task:
                student = adapter["quality"]
            else:
                student = max(0.42, adapter["quality"] - rng.uniform(0.08, 0.31))
            slice_scores.append(
                {
                    "slice": name,
                    "student_score": round(student, 4),
                    "teacher_score": spec["teacher"],
                    "sample_count": spec["cases"],
                }
            )

        records.append(
            {
                "id": adapter["id"],
                "name": adapter["id"].removeprefix("adp-").replace("-", " ").title(),
                "task": task,
                "base_model": "meta-llama/Llama-3.1-8B-Instruct",
                "rank": adapter["rank"],
                "alpha": adapter["rank"] * 2,
                "quality_score": adapter["quality"],
                "parity": round(adapter["quality"] / teacher, 4),
                "request_share": adapter["share"],
                "requests_24h": int(total_requests_24h * adapter["share"]),
                "trained_at": _iso(ANCHOR - dt.timedelta(days=7 + index * 3)),
                "training_examples": 4_000 + index * 1_450,
                "status": "active" if index < 10 else "shadow",
                "artifact_uri": f"s3://distillserve-adapters/{adapter['id']}/v1",
                "slices": slice_scores,
            }
        )

    share_total = sum(a["request_share"] for a in records)
    if abs(share_total - 1.0) > 0.005:
        raise ValueError(f"adapter request_share must sum to 1.0, got {share_total:.4f}")

    return {
        "source_id": config["source_id"],
        "description": "Twelve LoRA adapters with per-slice quality and request volume.",
        "adapters": records,
    }


def build_registry(config: dict[str, Any]) -> dict[str, Any]:
    """Emit the fifteen model-registry entries."""
    head = config["headline"]
    entries: list[dict[str, Any]] = []

    for index, run in enumerate(config["registry"]):
        family = run["family"]
        base = (
            "groq/llama-3.3-70b-versatile"
            if family == "teacher"
            else "meta-llama/Llama-3.1-8B-Instruct"
        )
        history = ["registered"]
        if run["status"] in {"promoted", "archived"}:
            history.append("passed CI gate")
        if run["status"] == "promoted":
            history.extend(["canary_50 clean", "promoted to full traffic"])
        if run["status"] == "archived" and index > 1:
            history.append("superseded")

        entries.append(
            {
                "id": run["id"],
                "name": run["id"].removeprefix("mv-").replace("-", " ").title(),
                "family": family,
                "base_model": base,
                "version": run["version"],
                "created_at": _iso(ANCHOR - dt.timedelta(days=90 - index * 6)),
                # blake2b, not the builtin hash(): PYTHONHASHSEED randomises
                # str hashing per process, which would make the dataset
                # non-deterministic and break the CI drift check.
                "git_sha": hashlib.blake2b(run["id"].encode(), digest_size=6).hexdigest(),
                "dataset_version": f"traces-2026-{6 + index % 3:02d}",
                "seed": SEED + index,
                "hyperparameters": {
                    "lora_rank": "32" if family != "teacher" else "n/a",
                    "lora_alpha": "64" if family != "teacher" else "n/a",
                    "learning_rate": "1e-4",
                    "epochs": "3",
                    "batch_size": "16",
                    "quantization": (
                        "fp8"
                        if "fp8" in run["version"]
                        else ("int4" if "int4" in run["version"] else "bf16")
                    ),
                    "objective": "dpo" if family == "dpo" else "sft-distillation",
                },
                "quality_score": round(run["parity"] * head["quality_parity"] / 0.974, 4),
                "parity": run["parity"],
                "p95_ttft_ms": run["ttft"],
                "usd_per_million_tokens": run["cost"],
                "status": run["status"],
                "promotion_history": history,
            }
        )

    return {
        "source_id": config["source_id"],
        "description": (
            "Fifteen model runs: teacher, distilled students, quantized and DPO variants."
        ),
        "versions": entries,
    }


def build_evals(config: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Emit a pre-computed eval report per model version."""
    evals = config["evals"]
    head = config["headline"]
    registry = {r["id"]: r for r in config["registry"]}

    pareto_source = [
        {
            "model": run["id"],
            "quality": run["parity"],
            "p95_ttft_ms": run["ttft"],
            "usd_per_million_tokens": run["cost"],
        }
        for run in config["registry"]
    ]
    # A point is on the frontier when nothing else is better on every axis.
    for point in pareto_source:
        point["on_frontier"] = not any(
            other["quality"] >= point["quality"]
            and other["p95_ttft_ms"] <= point["p95_ttft_ms"]
            and other["usd_per_million_tokens"] <= point["usd_per_million_tokens"]
            and other is not point
            and (
                other["quality"] > point["quality"]
                or other["p95_ttft_ms"] < point["p95_ttft_ms"]
                or other["usd_per_million_tokens"] < point["usd_per_million_tokens"]
            )
            for other in pareto_source
        )

    reports: list[dict[str, Any]] = []
    for run_id, run in registry.items():
        # The teacher is graded against itself, so every slice is exactly at
        # parity. Scaling it like a student produced cells above 100%, which is
        # not a strong result — it is a broken measurement.
        is_teacher = run["family"] == "teacher"
        ratio = run["parity"] / head["quality_parity"]
        slices = [
            {
                "slice": spec["name"],
                "student_score": (
                    spec["teacher"]
                    if is_teacher
                    # A student can legitimately edge past the teacher on an
                    # easy slice, so parity is capped just above 1.0 rather
                    # than at it — but not by a margin that would flatter it.
                    else round(min(spec["teacher"] * 1.01, spec["student"] * ratio), 4)
                ),
                "teacher_score": spec["teacher"],
                "sample_count": spec["cases"],
            }
            for spec in config["slices"]
        ]
        judge = [
            {
                "slice": spec["name"],
                "student_wins": int(spec["cases"] * (0.30 + 0.22 * ratio)),
                "teacher_wins": int(spec["cases"] * (0.38 - 0.16 * ratio)),
                "ties": max(
                    0,
                    spec["cases"]
                    - int(spec["cases"] * (0.30 + 0.22 * ratio))
                    - int(spec["cases"] * (0.38 - 0.16 * ratio)),
                ),
            }
            for spec in config["slices"]
        ]
        adversarial = [
            {
                "suite": suite["suite"],
                "attempts": suite["attempts"],
                "blocked": suite["blocked"],
                "passed_through": suite["attempts"] - suite["blocked"],
            }
            for suite in evals["adversarial"]
        ]

        reports.append(
            {
                "model_version_id": run_id,
                "model_name": run_id.removeprefix("mv-").replace("-", " ").title(),
                "generated_at": _iso(ANCHOR - dt.timedelta(days=2)),
                "dataset_version": "traces-2026-08",
                "case_count": head["test_set_cases"],
                "overall_parity": run["parity"],
                "slices": slices,
                "judge": judge,
                "adversarial": adversarial,
                "pareto": pareto_source,
                "gate_threshold": evals["gate_threshold"],
                "judge_model": evals["judge_model"],
                "judge_drift": evals["judge_drift"],
            }
        )

    del rng
    return {
        "source_id": config["source_id"],
        "description": "Pre-computed eval reports for every model version.",
        "calibration_cases": evals["calibration_cases"],
        "reports": reports,
    }


def build_rollouts(config: dict[str, Any]) -> dict[str, Any]:
    """Emit rollout records with their SLIs."""
    head = config["headline"]
    gate = config["evals"]["gate_threshold"]
    registry = {r["id"]: r for r in config["registry"]}

    stage_traffic = {
        "shadow": 0,
        "canary_10": 10,
        "canary_50": 50,
        "full": 100,
        "rolled_back": 0,
    }

    records: list[dict[str, Any]] = []
    for index, rollout in enumerate(config["rollouts"]):
        candidate = registry[rollout["candidate"]]
        parity = candidate["parity"]
        ttft = candidate["ttft"]
        breached = rollout["stage"] == "rolled_back"

        records.append(
            {
                "id": rollout["id"],
                "name": rollout["name"],
                "candidate_model": rollout["candidate"],
                "baseline_model": rollout["baseline"],
                "stage": rollout["stage"],
                "traffic_percent": stage_traffic[rollout["stage"]],
                "started_at": _iso(ANCHOR - dt.timedelta(days=9 - index * 2)),
                "updated_at": _iso(ANCHOR - dt.timedelta(days=1, hours=index * 3)),
                "auto_rollback": True,
                "notes": rollout["notes"],
                "slis": [
                    {
                        "name": "quality_parity",
                        "value": 0.891 if breached else parity,
                        "threshold": gate,
                        "unit": "ratio",
                        "comparison": "gte",
                        "status": "breached" if breached else "healthy",
                    },
                    {
                        "name": "p95_ttft",
                        "value": float(ttft),
                        "threshold": float(head["p95_ttft_ms"] + 40),
                        "unit": "ms",
                        "comparison": "lte",
                        "status": "healthy" if ttft <= head["p95_ttft_ms"] + 40 else "at_risk",
                    },
                    {
                        "name": "error_rate",
                        "value": 0.0012,
                        "threshold": 0.01,
                        "unit": "ratio",
                        "comparison": "lte",
                        "status": "healthy",
                    },
                ],
            }
        )

    return {
        "source_id": config["source_id"],
        "description": "Canary rollouts and their service-level indicators.",
        "rollouts": records,
    }


def build_tenants(config: dict[str, Any]) -> dict[str, Any]:
    """Emit the per-tenant usage breakdown."""
    head = config["headline"]
    work = config["workload"]
    daily_requests = int(work["requests_per_second_mean"] * 86_400)

    records = []
    for tenant in config["tenants"]:
        requests = int(daily_requests * tenant["share"])
        output_tokens = requests * work["mean_output_tokens"]
        # Student traffic is cheap, teacher traffic is not; blending them is
        # what makes the per-tenant cost column differ from a flat rate.
        blended = head["usd_per_million_tokens"] * tenant["student_share"] + head[
            "frontier_usd_per_million_tokens"
        ] * (1 - tenant["student_share"])
        records.append(
            {
                "tenant_id": tenant["id"],
                "name": tenant["name"],
                "requests": requests,
                "input_tokens": requests * work["mean_input_tokens"],
                "output_tokens": output_tokens,
                "cost_usd": round(output_tokens / 1_000_000 * blended, 2),
                "cache_hit_rate": round(head["cache_hit_rate"] * (0.8 + tenant["share"]), 4),
                "p95_ttft_ms": round(head["p95_ttft_ms"] * (0.92 + tenant["share"] * 0.3), 1),
                "student_share": tenant["student_share"],
            }
        )

    return {
        "source_id": config["source_id"],
        "description": "Per-tenant usage and cost attribution over 24h.",
        "tenants": records,
    }


def build_all() -> list[Path]:
    """Regenerate every payload. Returns the files written."""
    config = _load()
    rng = random.Random(SEED)  # noqa: S311 - dataset generation, not cryptography

    written = [
        _write("serving_trace.json", build_serving_trace(config, rng)),
        _write("cluster_events.json", build_cluster_events(config, rng)),
        _write("adapters.json", build_adapters(config, rng)),
        _write("model_registry.json", build_registry(config)),
        _write("eval_reports.json", build_evals(config, rng)),
        _write("rollouts.json", build_rollouts(config)),
        _write("tenants.json", build_tenants(config)),
    ]
    return written


if __name__ == "__main__":
    for path in build_all():
        print(f"wrote {path.relative_to(REPO_ROOT)}")
