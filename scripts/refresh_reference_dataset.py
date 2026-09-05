"""Validate the reference dataset's source configs and rebuild its manifest.

``data/reference/`` backs ``DISTILLSERVE_MODE=sandbox``: it is where
``ReferenceBenchmarkStore`` reads throughput/TTFT/ITL curves, KV-pressure and
GPU-utilisation traces, rollout history, adapter records and model-registry
entries from. This script is the dataset's build step.

What it enforces today:

* every source in ``sources/catalog.yaml`` parses and carries a citation
  (title, publisher, URL, publication date, licence, and what it provides);
* the reference deployment shape is fully specified, because every derived
  rate in the dataset is computed against it;
* every data file in ``data/reference/`` declares a ``source_id`` that exists
  in the catalog — data without provenance fails the build;
* the manifest records a SHA-256 per file plus a dataset-wide digest, so a
  dashboard number can be tied to an exact dataset revision.

Usage:
    uv run python scripts/refresh_reference_dataset.py
    uv run python scripts/refresh_reference_dataset.py --check   # CI: fail on drift
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import sys as _sys
from pathlib import Path
from pathlib import Path as _Path
from typing import Any

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import yaml
from build_reference_payloads import build_all
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "reference"
CATALOG = DATA_DIR / "sources" / "catalog.yaml"
GENERATOR = DATA_DIR / "generator.yaml"
MANIFEST = DATA_DIR / "manifest.json"

#: Files under data/reference/ that are inputs or outputs of this script rather
#: than dataset payloads, and therefore carry no ``source_id`` of their own.
_NON_PAYLOAD = {CATALOG, MANIFEST}


class Source(BaseModel):
    """A citation for part of the reference dataset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str
    publisher: str
    url: HttpUrl | str
    published: dt.date
    retrieved: dt.date
    license: str
    provides: list[str] = Field(min_length=1, description="What this source backs in the dataset.")


class ReferenceDeployment(BaseModel):
    """The cluster shape the reference telemetry represents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accelerator: str
    gpu_count: int = Field(gt=0)
    tensor_parallel_size: int = Field(gt=0)
    replicas: int = Field(gt=0)
    serving_engine: str
    weight_dtype: str
    kv_cache_dtype: str
    speculative_decoding: str
    prefix_caching: bool
    scheduler: str


class Catalog(BaseModel):
    """Top-level shape of ``sources/catalog.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    sources: list[Source] = Field(min_length=1)
    reference_deployment: ReferenceDeployment


def load_catalog() -> Catalog:
    """Parse and validate the source catalog."""
    raw = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return Catalog.model_validate(raw)


def _sha256(path: Path) -> str:
    """Return the SHA-256 of a file, read in chunks so large traces stay cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _declared_source_id(path: Path) -> str | None:
    """Return the ``source_id`` a payload file declares, if it declares one."""
    try:
        if path.suffix in {".yaml", ".yml"}:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif path.suffix == ".json":
            document = json.loads(path.read_text(encoding="utf-8"))
        else:
            return None
    except (yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(document, dict):
        value = document.get("source_id")
        return value if isinstance(value, str) else None
    return None


def collect_payloads(catalog: Catalog) -> list[dict[str, Any]]:
    """Hash every dataset payload and verify its declared provenance.

    Raises:
        ValueError: when a payload declares no ``source_id``, or declares one
            that is absent from the catalog.
    """
    known = {source.id for source in catalog.sources}
    entries: list[dict[str, Any]] = []

    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file() or path in _NON_PAYLOAD:
            continue
        source_id = _declared_source_id(path)
        relative = path.relative_to(REPO_ROOT).as_posix()
        if source_id is None:
            raise ValueError(f"{relative} declares no `source_id`; every payload needs provenance.")
        if source_id not in known:
            raise ValueError(f"{relative} cites unknown source_id {source_id!r}.")
        entries.append(
            {
                "path": relative,
                "source_id": source_id,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return entries


def build_manifest() -> dict[str, Any]:
    """Assemble the manifest describing the current dataset revision."""
    catalog = load_catalog()
    payloads = collect_payloads(catalog)

    # The dataset digest is a hash of the per-file hashes, so it changes if and
    # only if some payload changed. It is what the store reports alongside
    # telemetry, letting a screenshot be pinned to an exact dataset revision.
    rollup = hashlib.sha256()
    for entry in payloads:
        rollup.update(f"{entry['path']}:{entry['sha256']}".encode())

    return {
        "catalog_version": catalog.version,
        "dataset_digest": rollup.hexdigest(),
        "reference_deployment": catalog.reference_deployment.model_dump(),
        "sources": [json.loads(source.model_dump_json()) for source in catalog.sources],
        "payloads": payloads,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the checked-in manifest differs from a fresh build.",
    )
    args = parser.parse_args(argv)

    # Payloads are regenerated first so the manifest always describes what is
    # actually on disk. In --check mode they are regenerated too: that is the
    # drift test, since a changed generator.yaml must change the digest.
    build_all()

    try:
        manifest = build_manifest()
    except (ValidationError, ValueError) as exc:
        print(f"Reference dataset is invalid:\n{exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    if args.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if current != rendered:
            print(
                "data/reference/manifest.json is stale. Run `make reference-refresh`.",
                file=sys.stderr,
            )
            return 1
        print("data/reference/manifest.json is up to date.")
        return 0

    MANIFEST.write_text(rendered, encoding="utf-8", newline="\n")
    print(
        f"Wrote {MANIFEST.relative_to(REPO_ROOT)} — "
        f"{len(manifest['sources'])} sources, {len(manifest['payloads'])} payloads, "
        f"digest {manifest['dataset_digest'][:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
