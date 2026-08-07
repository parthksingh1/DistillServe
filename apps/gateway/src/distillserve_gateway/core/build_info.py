"""Resolution of the commit this process was built from.

Deployment platforms hand the SHA to the process as an env var and then throw
the ``.git`` directory away, so the env var is authoritative. The `git`
subprocess is only a local-development convenience, and it is executed exactly
once at import time — a health probe must never fork.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

UNKNOWN_SHA = "unknown"

#: Build env vars in precedence order. Render and Vercel each inject their own;
#: ``GIT_SHA`` is the escape hatch for Docker builds and CI.
_SHA_ENV_VARS: tuple[str, ...] = (
    "DISTILLSERVE_GIT_SHA",
    "RENDER_GIT_COMMIT",
    "VERCEL_GIT_COMMIT_SHA",
    "GITHUB_SHA",
    "GIT_SHA",
)


def _sha_from_env() -> str | None:
    """Return the first non-empty build SHA env var, if any."""
    for name in _SHA_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _sha_from_git() -> str | None:
    """Return the working tree's HEAD SHA, or None when unavailable."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — resolved via PATH by design.
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else None


@lru_cache(maxsize=1)
def git_sha() -> str:
    """Return the full commit SHA, or ``"unknown"`` outside a checkout."""
    return _sha_from_env() or _sha_from_git() or UNKNOWN_SHA


def git_sha_short(length: int = 12) -> str:
    """Return the first ``length`` characters of :func:`git_sha`."""
    return git_sha()[:length]
