r"""The canary rollout controller.

Modelled on a Kubernetes controller rather than a workflow engine: there is a
*desired* stage and an *observed* set of SLIs, and :meth:`reconcile` drives one
toward the other. The distinction matters operationally — a workflow that
"runs" a rollout has to be restarted after a crash and can be run twice, while
a reconciliation loop is idempotent and can be interrupted at any point.

The state machine is deliberately asymmetric:

    shadow -> canary_10 -> canary_50 -> full
      \\_________________________________/
                     v
                rolled_back

Advancing goes one stage at a time, because each stage is a distinct amount of
exposure and you want to observe at each. Rolling back jumps straight to
``rolled_back`` from anywhere: a quality incident is not something you unwind
gradually while users keep receiving bad output.

Every decision — automatic or human — is written to a hash-chained audit log.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from distillserve_otel import get_logger
from distillserve_schemas import (
    STAGE_TRAFFIC,
    Rollout,
    RolloutEvent,
    RolloutSLI,
    RolloutStage,
    SLIStatus,
)

log = get_logger(__name__)

#: The forward path. Rollback is not in here because it is reachable from any
#: stage, which is the point.
ADVANCE_ORDER: tuple[RolloutStage, ...] = (
    RolloutStage.SHADOW,
    RolloutStage.CANARY_10,
    RolloutStage.CANARY_50,
    RolloutStage.FULL,
)


class RolloutTransitionError(RuntimeError):
    """An illegal or unsafe stage transition was requested."""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one reconciliation pass decided."""

    rollout_id: str
    stage: RolloutStage
    changed: bool
    reason: str
    breached_slis: tuple[str, ...] = ()


class AuditLog:
    """Append-only, hash-chained rollout audit log in SQLite.

    Each row's signature is an HMAC over its own content *and the previous
    row's signature*. Removing or editing a row breaks every signature after
    it, so tampering is detectable rather than merely discouraged — which is
    the difference between an audit log and a log.

    The HMAC key comes from configuration. Without one the log still chains
    (so ordering and completeness are verifiable) but cannot prove authorship;
    :meth:`verify` reports that distinction rather than hiding it.
    """

    def __init__(self, db_path: Path | str, *, secret: str | None = None) -> None:
        """Open (and create if needed) the audit database."""
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = (secret or "distillserve-unsigned").encode("utf-8")
        self._signed = secret is not None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS rollout_audit (
                    id                 TEXT PRIMARY KEY,
                    rollout_id         TEXT NOT NULL,
                    at                 TEXT NOT NULL,
                    kind               TEXT NOT NULL,
                    from_stage         TEXT,
                    to_stage           TEXT,
                    actor              TEXT NOT NULL,
                    reason             TEXT NOT NULL,
                    signature          TEXT NOT NULL,
                    previous_signature TEXT
                )
                """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_rollout ON rollout_audit(rollout_id, at)"
            )

    def _sign(self, payload: dict[str, str | None], previous: str | None) -> str:
        """HMAC the entry's content chained to the previous signature."""
        message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        chained = f"{previous or ''}|{message}".encode()
        return hmac.new(self._secret, chained, hashlib.sha256).hexdigest()

    def latest_signature(self, rollout_id: str) -> str | None:
        """Return the most recent signature for ``rollout_id``."""
        with self._connect() as db:
            row = db.execute(
                "SELECT signature FROM rollout_audit WHERE rollout_id = ?"
                " ORDER BY at DESC, id DESC LIMIT 1",
                (rollout_id,),
            ).fetchone()
        return str(row["signature"]) if row else None

    def append(
        self,
        *,
        rollout_id: str,
        kind: str,
        actor: str,
        reason: str,
        from_stage: RolloutStage | None = None,
        to_stage: RolloutStage | None = None,
        at: dt.datetime | None = None,
    ) -> RolloutEvent:
        """Write one audit entry and return it."""
        moment = at or dt.datetime.now(dt.UTC)
        previous = self.latest_signature(rollout_id)
        payload: dict[str, str | None] = {
            "rollout_id": rollout_id,
            "at": moment.isoformat(),
            "kind": kind,
            "from_stage": from_stage.value if from_stage else None,
            "to_stage": to_stage.value if to_stage else None,
            "actor": actor,
            "reason": reason,
        }
        signature = self._sign(payload, previous)
        entry_id = f"aud-{uuid.uuid4().hex[:16]}"

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO rollout_audit
                    (id, rollout_id, at, kind, from_stage, to_stage, actor, reason,
                     signature, previous_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    rollout_id,
                    payload["at"],
                    kind,
                    payload["from_stage"],
                    payload["to_stage"],
                    actor,
                    reason,
                    signature,
                    previous,
                ),
            )

        return RolloutEvent(
            id=entry_id,
            rollout_id=rollout_id,
            at=moment,
            kind=kind,
            from_stage=from_stage,
            to_stage=to_stage,
            actor=actor,
            reason=reason,
            signature=signature,
            previous_signature=previous,
        )

    def entries(self, rollout_id: str | None = None, limit: int = 200) -> list[RolloutEvent]:
        """Return audit entries, newest first."""
        query = "SELECT * FROM rollout_audit"
        args: tuple[str, ...] = ()
        if rollout_id is not None:
            query += " WHERE rollout_id = ?"
            args = (rollout_id,)
        query += " ORDER BY at DESC, id DESC LIMIT ?"

        with self._connect() as db:
            rows = db.execute(query, (*args, limit)).fetchall()

        return [
            RolloutEvent(
                id=row["id"],
                rollout_id=row["rollout_id"],
                at=dt.datetime.fromisoformat(row["at"]),
                kind=row["kind"],
                from_stage=RolloutStage(row["from_stage"]) if row["from_stage"] else None,
                to_stage=RolloutStage(row["to_stage"]) if row["to_stage"] else None,
                actor=row["actor"],
                reason=row["reason"],
                signature=row["signature"],
                previous_signature=row["previous_signature"],
            )
            for row in rows
        ]

    def verify(self, rollout_id: str) -> tuple[bool, str]:
        """Recompute the chain and report whether it is intact."""
        entries = sorted(self.entries(rollout_id, limit=10_000), key=lambda e: e.at)
        previous: str | None = None

        for entry in entries:
            payload: dict[str, str | None] = {
                "rollout_id": entry.rollout_id,
                "at": entry.at.isoformat(),
                "kind": entry.kind,
                "from_stage": entry.from_stage.value if entry.from_stage else None,
                "to_stage": entry.to_stage.value if entry.to_stage else None,
                "actor": entry.actor,
                "reason": entry.reason,
            }
            if not hmac.compare_digest(self._sign(payload, previous), entry.signature):
                return False, f"chain broken at {entry.id} ({entry.at.isoformat()})"
            previous = entry.signature

        kind = "signed" if self._signed else "chained but unsigned (no audit secret configured)"
        return True, f"{len(entries)} entries verified, {kind}"


class RolloutController:
    """Drives rollouts through the canary state machine."""

    def __init__(self, audit: AuditLog, rollouts: Sequence[Rollout]) -> None:
        """Seed the controller with the current rollout set."""
        self._audit = audit
        self._rollouts: dict[str, Rollout] = {r.id: r for r in rollouts}

    def list_rollouts(self) -> list[Rollout]:
        """All rollouts, newest first."""
        return sorted(self._rollouts.values(), key=lambda r: r.started_at, reverse=True)

    def get(self, rollout_id: str) -> Rollout | None:
        """One rollout."""
        return self._rollouts.get(rollout_id)

    def _require(self, rollout_id: str) -> Rollout:
        rollout = self._rollouts.get(rollout_id)
        if rollout is None:
            raise RolloutTransitionError(f"No rollout with id {rollout_id!r}")
        return rollout

    @staticmethod
    def next_stage(stage: RolloutStage) -> RolloutStage | None:
        """The stage after ``stage``, or None at the end of the line."""
        if stage not in ADVANCE_ORDER:
            return None
        index = ADVANCE_ORDER.index(stage)
        return ADVANCE_ORDER[index + 1] if index + 1 < len(ADVANCE_ORDER) else None

    @staticmethod
    def breached(slis: Sequence[RolloutSLI]) -> tuple[str, ...]:
        """Names of the SLIs currently outside their objective."""
        return tuple(s.name for s in slis if s.status is SLIStatus.BREACHED)

    def advance(self, rollout_id: str, *, actor: str, reason: str = "") -> Rollout:
        """Move a rollout one stage forward.

        Refuses while any SLI is breached. Advancing a rollout whose quality
        gate is failing is the single most expensive mistake this controller
        can permit, so it is blocked here rather than left to the operator's
        judgement — the override path is to fix or re-baseline the SLI.

        Raises:
            RolloutTransitionError: at the end of the line, from a rolled-back
                state, or while an SLI is breached.
        """
        rollout = self._require(rollout_id)

        if rollout.stage is RolloutStage.ROLLED_BACK:
            raise RolloutTransitionError(
                f"{rollout_id} was rolled back; start a new rollout rather than resuming it."
            )

        breached = self.breached(rollout.slis)
        if breached:
            raise RolloutTransitionError(
                f"Cannot advance {rollout_id}: SLI(s) breached: {', '.join(breached)}."
            )

        target = self.next_stage(rollout.stage)
        if target is None:
            raise RolloutTransitionError(f"{rollout_id} is already at {rollout.stage.value}.")

        return self._transition(
            rollout,
            target,
            kind="advance",
            actor=actor,
            reason=reason or f"advanced {rollout.stage.value} -> {target.value}",
        )

    def rollback(self, rollout_id: str, *, actor: str, reason: str) -> Rollout:
        """Return a rollout to baseline immediately, from any stage."""
        rollout = self._require(rollout_id)
        if rollout.stage is RolloutStage.ROLLED_BACK:
            raise RolloutTransitionError(f"{rollout_id} is already rolled back.")

        return self._transition(
            rollout, RolloutStage.ROLLED_BACK, kind="rollback", actor=actor, reason=reason
        )

    def reconcile(self, rollout_id: str) -> ReconcileResult:
        """Run one reconciliation pass.

        Auto-rollback on a breached SLI is the whole reason this loop exists:
        a human noticing a quality regression on a dashboard is minutes of bad
        output, while the controller notices within one interval.
        """
        rollout = self._require(rollout_id)
        breached = self.breached(rollout.slis)

        if not breached:
            return ReconcileResult(rollout_id, rollout.stage, False, "all SLIs within objective")

        if rollout.stage is RolloutStage.ROLLED_BACK:
            return ReconcileResult(
                rollout_id, rollout.stage, False, "already rolled back", breached
            )

        if not rollout.auto_rollback:
            return ReconcileResult(
                rollout_id,
                rollout.stage,
                False,
                f"SLI(s) breached ({', '.join(breached)}) but auto-rollback is disabled",
                breached,
            )

        reason = f"auto-rollback: SLI breach on {', '.join(breached)}"
        updated = self._transition(
            rollout,
            RolloutStage.ROLLED_BACK,
            kind="auto_rollback",
            actor="controller",
            reason=reason,
        )
        return ReconcileResult(rollout_id, updated.stage, True, reason, breached)

    def _transition(
        self, rollout: Rollout, target: RolloutStage, *, kind: str, actor: str, reason: str
    ) -> Rollout:
        """Apply a stage change and write the audit entry."""
        self._audit.append(
            rollout_id=rollout.id,
            kind=kind,
            actor=actor,
            reason=reason,
            from_stage=rollout.stage,
            to_stage=target,
        )
        updated = rollout.model_copy(
            update={
                "stage": target,
                "traffic_percent": STAGE_TRAFFIC[target],
                "updated_at": dt.datetime.now(dt.UTC),
            }
        )
        self._rollouts[rollout.id] = updated
        log.info(
            "rollout.transition",
            rollout=rollout.id,
            kind=kind,
            from_stage=rollout.stage.value,
            to_stage=target.value,
            actor=actor,
        )
        return updated

    def audit(self, rollout_id: str | None = None, limit: int = 100) -> list[RolloutEvent]:
        """Audit entries for a rollout, or across all of them."""
        return self._audit.entries(rollout_id, limit)

    def verify_audit(self, rollout_id: str) -> tuple[bool, str]:
        """Verify one rollout's audit chain."""
        return self._audit.verify(rollout_id)
