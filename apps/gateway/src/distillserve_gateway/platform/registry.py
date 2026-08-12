"""SQLite-backed model registry, adapter registry and eval store.

One SQLite file holds model versions, adapters and eval reports. SQLite rather
than Postgres because this data is small, read-mostly, and its value is being
*there* — a registry you have to provision a database for is a registry that
does not exist during the first month of a project.

The registry is seeded from the reference dataset on first run and is
authoritative afterwards, so a hot-swap or a promotion performed through the UI
persists across restarts rather than being reset by the seed.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from distillserve_otel import get_logger
from distillserve_schemas import Adapter, EvalReport, ModelVersion

log = get_logger(__name__)


class PlatformStore:
    """Persistent home for adapters, model versions and eval reports."""

    def __init__(self, db_path: Path | str) -> None:
        """Open (and create if needed) the platform database."""
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        """Create tables.

        Documents are stored as JSON in a ``document`` column with the query
        keys lifted out into real columns. The lifted columns are what the UI
        sorts and filters on; the JSON is the Pydantic model verbatim, so a new
        field on a schema does not need a migration to be readable.
        """
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_versions (
                    id        TEXT PRIMARY KEY,
                    name      TEXT NOT NULL,
                    family    TEXT NOT NULL,
                    version   TEXT NOT NULL,
                    status    TEXT NOT NULL,
                    parity    REAL NOT NULL,
                    p95_ttft_ms REAL NOT NULL,
                    usd_per_million_tokens REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    document  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adapters (
                    id       TEXT PRIMARY KEY,
                    task     TEXT NOT NULL,
                    status   TEXT NOT NULL,
                    quality_score REAL NOT NULL,
                    request_share REAL NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_reports (
                    model_version_id TEXT PRIMARY KEY,
                    generated_at     TEXT NOT NULL,
                    overall_parity   REAL NOT NULL,
                    document         TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mv_status ON model_versions(status);
                CREATE INDEX IF NOT EXISTS idx_adapters_task ON adapters(task);
                """)

    # -- seeding ------------------------------------------------------------

    def is_empty(self) -> bool:
        """True when nothing has been seeded yet."""
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM model_versions").fetchone()
        return int(row["n"]) == 0

    def seed(
        self,
        *,
        model_versions: Sequence[ModelVersion],
        adapters: Sequence[Adapter],
        eval_reports: Sequence[EvalReport],
    ) -> None:
        """Populate an empty store from the reference dataset.

        Only runs when the store is empty. Re-seeding on every boot would
        silently discard a promotion or a hot-swap performed through the UI,
        which is the kind of bug that makes an admin console untrustworthy.
        """
        if not self.is_empty():
            log.debug("registry.seed_skipped", reason="store already populated")
            return

        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO model_versions
                    (id, name, family, version, status, parity, p95_ttft_ms,
                     usd_per_million_tokens, created_at, document)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        v.id,
                        v.name,
                        v.family,
                        v.version,
                        v.status,
                        v.parity,
                        v.p95_ttft_ms,
                        v.usd_per_million_tokens,
                        v.created_at.isoformat(),
                        v.model_dump_json(),
                    )
                    for v in model_versions
                ],
            )
            db.executemany(
                """
                INSERT INTO adapters
                    (id, task, status, quality_score, request_share, document)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (a.id, a.task, a.status, a.quality_score, a.request_share, a.model_dump_json())
                    for a in adapters
                ],
            )
            db.executemany(
                """
                INSERT INTO eval_reports
                    (model_version_id, generated_at, overall_parity, document)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        r.model_version_id,
                        r.generated_at.isoformat(),
                        r.overall_parity,
                        r.model_dump_json(),
                    )
                    for r in eval_reports
                ],
            )

        log.info(
            "registry.seeded",
            model_versions=len(model_versions),
            adapters=len(adapters),
            eval_reports=len(eval_reports),
        )

    # -- model versions -----------------------------------------------------

    def list_model_versions(
        self, *, family: str | None = None, status: str | None = None
    ) -> list[ModelVersion]:
        """List model versions, newest first."""
        query = "SELECT document FROM model_versions"
        clauses: list[str] = []
        args: list[Any] = []
        if family:
            clauses.append("family = ?")
            args.append(family)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"

        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        return [ModelVersion.model_validate_json(row["document"]) for row in rows]

    def model_version(self, version_id: str) -> ModelVersion | None:
        """One model version."""
        with self._connect() as db:
            row = db.execute(
                "SELECT document FROM model_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return ModelVersion.model_validate_json(row["document"]) if row else None

    def promote(self, version_id: str, *, actor: str) -> ModelVersion:
        """Promote a version, demoting whichever one currently holds the slot.

        Promotion is exclusive within a family for a reason: two "promoted"
        students is an ambiguous routing target, and the router would have to
        guess.

        Raises:
            KeyError: when the version does not exist.
        """
        version = self.model_version(version_id)
        if version is None:
            raise KeyError(f"No model version {version_id!r}")

        now = dt.datetime.now(dt.UTC).isoformat()
        with self._connect() as db:
            for row in db.execute(
                "SELECT document FROM model_versions WHERE family = ? AND status = 'promoted'",
                (version.family,),
            ).fetchall():
                current = ModelVersion.model_validate_json(row["document"])
                demoted = current.model_copy(
                    update={
                        "status": "archived",
                        "promotion_history": [
                            *current.promotion_history,
                            f"superseded by {version_id} at {now}",
                        ],
                    }
                )
                db.execute(
                    "UPDATE model_versions SET status = ?, document = ? WHERE id = ?",
                    ("archived", demoted.model_dump_json(), current.id),
                )

            promoted = version.model_copy(
                update={
                    "status": "promoted",
                    "promotion_history": [
                        *version.promotion_history,
                        f"promoted by {actor} at {now}",
                    ],
                }
            )
            db.execute(
                "UPDATE model_versions SET status = ?, document = ? WHERE id = ?",
                ("promoted", promoted.model_dump_json(), version_id),
            )

        log.info("registry.promoted", version=version_id, family=version.family, actor=actor)
        return promoted

    # -- adapters -----------------------------------------------------------

    def list_adapters(self, *, task: str | None = None) -> list[Adapter]:
        """List adapters, highest request share first."""
        query = "SELECT document FROM adapters"
        args: list[Any] = []
        if task:
            query += " WHERE task = ?"
            args.append(task)
        query += " ORDER BY request_share DESC"

        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        return [Adapter.model_validate_json(row["document"]) for row in rows]

    def adapter(self, adapter_id: str) -> Adapter | None:
        """One adapter."""
        with self._connect() as db:
            row = db.execute("SELECT document FROM adapters WHERE id = ?", (adapter_id,)).fetchone()
        return Adapter.model_validate_json(row["document"]) if row else None

    def set_adapter_status(self, adapter_id: str, status: str, *, actor: str) -> Adapter:
        """Hot-swap an adapter between ``active``, ``shadow`` and ``retired``.

        Raises:
            KeyError: when the adapter does not exist.
            ValueError: on an unknown status.
        """
        if status not in {"active", "shadow", "retired"}:
            raise ValueError(f"status must be active, shadow or retired; got {status!r}")

        adapter = self.adapter(adapter_id)
        if adapter is None:
            raise KeyError(f"No adapter {adapter_id!r}")

        updated = adapter.model_copy(update={"status": status})
        with self._connect() as db:
            db.execute(
                "UPDATE adapters SET status = ?, document = ? WHERE id = ?",
                (status, updated.model_dump_json(), adapter_id),
            )

        log.info("registry.adapter_swapped", adapter=adapter_id, status=status, actor=actor)
        return updated

    # -- eval reports -------------------------------------------------------

    def list_eval_reports(self) -> list[EvalReport]:
        """All eval reports, newest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT document FROM eval_reports ORDER BY generated_at DESC"
            ).fetchall()
        return [EvalReport.model_validate_json(row["document"]) for row in rows]

    def eval_report(self, version_id: str) -> EvalReport | None:
        """One eval report."""
        with self._connect() as db:
            row = db.execute(
                "SELECT document FROM eval_reports WHERE model_version_id = ?", (version_id,)
            ).fetchone()
        return EvalReport.model_validate_json(row["document"]) if row else None

    def upsert_eval_report(self, report: EvalReport) -> None:
        """Store a freshly computed eval report, replacing any previous one."""
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO eval_reports (model_version_id, generated_at, overall_parity, document)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model_version_id) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    overall_parity = excluded.overall_parity,
                    document = excluded.document
                """,
                (
                    report.model_version_id,
                    report.generated_at.isoformat(),
                    report.overall_parity,
                    report.model_dump_json(),
                ),
            )

    #: Tables :meth:`stats` counts. A fixed tuple, not a parameter: SQLite
    #: cannot bind a table name, so the only safe interpolation is one no
    #: caller can influence.
    _COUNTABLE_TABLES = ("model_versions", "adapters", "eval_reports")

    def stats(self) -> dict[str, int]:
        """Row counts, used by the readiness probe."""
        counts: dict[str, int] = {}
        with self._connect() as db:
            for table in self._COUNTABLE_TABLES:
                query = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - fixed tuple above
                counts[table] = int(db.execute(query).fetchone()["n"])
        return counts


def json_default(value: Any) -> str:
    """JSON encoder fallback for datetimes."""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialise {type(value)!r}")


__all__ = ["PlatformStore", "json"]
