"""Tenant authentication and scope enforcement.

DistillServe is multi-tenant from the first request: rate limits, cost
attribution and audit entries are all keyed on a tenant, so the tenant has to
be established before the pipeline runs rather than inferred afterwards.

Three decisions worth stating:

**Scopes, not roles, at the call site.** A route asks for ``Scope.OPERATE``;
it never asks whether the caller "is an operator". Adding a role later is then
a data change, not a code change across every endpoint.

**Tokens are compared by hash, in constant time.** The config holds SHA-256
digests, so a leaked tenants file does not leak working credentials, and
``compare_digest`` keeps the comparison from leaking token length or prefix
through timing.

**A demo tenant is a first-class tenant.** Showing the platform to someone
should not require handing over an admin credential or turning auth off. The
demo tenant holds ``READ`` and ``INFER`` — enough to drive the Playground and
every dashboard — and deliberately not ``OPERATE``, so a visitor cannot
advance a rollout or hot-swap an adapter.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Scope(StrEnum):
    """What a tenant is permitted to do."""

    READ = "read"
    """Read dashboards, traces, evals, adapters and the registry."""

    INFER = "infer"
    """Call /v1/chat/completions and the eval harness."""

    OPERATE = "operate"
    """Advance or roll back a rollout, hot-swap an adapter, promote a model."""

    ADMIN = "admin"
    """Manage tenants and platform configuration."""


#: Named scope bundles. Kept here rather than spelled out per tenant so that
#: changing what "operator" means is one edit rather than N.
ROLE_SCOPES: dict[str, frozenset[Scope]] = {
    "admin": frozenset(Scope),
    "operator": frozenset({Scope.READ, Scope.INFER, Scope.OPERATE}),
    "demo": frozenset({Scope.READ, Scope.INFER}),
    "viewer": frozenset({Scope.READ}),
}


class Tenant(BaseModel):
    """An authenticated caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    role: str = Field(description="Role name; must be a key of ROLE_SCOPES.")
    token_sha256: str | None = Field(
        default=None,
        description="SHA-256 of the tenant's bearer token. None means the "
        "tenant cannot authenticate over HTTP (used for the implicit local tenant).",
    )
    requests_per_minute: int = Field(
        default=60,
        gt=0,
        description="Token-bucket refill rate. The demo tenant is capped low so a "
        "shared link cannot exhaust the project's provider quota.",
    )
    monthly_budget_usd: float | None = Field(
        default=None,
        gt=0.0,
        description="Spend ceiling enforced by cost accounting. None means unlimited.",
    )

    @property
    def scopes(self) -> frozenset[Scope]:
        """Scopes granted by this tenant's role."""
        return ROLE_SCOPES.get(self.role, frozenset())

    def allows(self, scope: Scope) -> bool:
        """Whether this tenant holds ``scope``."""
        return scope in self.scopes


class TenantDirectory(BaseModel):
    """The set of tenants this gateway will authenticate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    tenants: list[Tenant] = Field(default_factory=list)

    def by_id(self, tenant_id: str) -> Tenant | None:
        """Look a tenant up by id."""
        return next((t for t in self.tenants if t.id == tenant_id), None)

    def authenticate(self, token: str) -> Tenant | None:
        """Resolve a bearer token to a tenant.

        Every candidate is compared even after a match so that the work done is
        independent of which tenant matched — the same reason the comparison
        itself is constant-time.
        """
        digest = token_digest(token)
        matched: Tenant | None = None
        for tenant in self.tenants:
            if tenant.token_sha256 is None:
                continue
            if hmac.compare_digest(tenant.token_sha256, digest):
                matched = tenant
        return matched


def token_digest(token: str) -> str:
    """Return the SHA-256 hex digest of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: The tenant requests run as when authentication is disabled. Full scopes,
#: because a developer running the stack locally is the operator.
LOCAL_TENANT = Tenant(
    id="local",
    name="Local development",
    role="admin",
    requests_per_minute=600,
)

#: Identity of the built-in demo tenant. Its token is configuration, not code:
#: see `demo_tenant()`.
DEMO_TENANT_ID = "demo"


def demo_tenant(token: str, *, requests_per_minute: int = 20) -> Tenant:
    """Build the demo tenant from a configured token.

    The token is intended to be shared — that is the point — so the tenant is
    scoped to ``READ`` and ``INFER`` and rate-limited well below a real one.
    Someone handed this token can drive the entire product and cannot change
    a rollout, an adapter or a model promotion.
    """
    return Tenant(
        id=DEMO_TENANT_ID,
        name="Demo access",
        role="demo",
        token_sha256=token_digest(token),
        requests_per_minute=requests_per_minute,
        monthly_budget_usd=5.0,
    )


def load_directory(path: Path | str | None) -> TenantDirectory:
    """Load tenants from YAML, or return an empty directory when absent.

    An absent file is not an error: the common deployment has exactly one
    demo tenant plus the operator's own key, both from environment variables.
    """
    if path is None:
        return TenantDirectory()
    resolved = Path(path)
    if not resolved.is_file():
        return TenantDirectory()
    raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    return TenantDirectory.model_validate(raw)


@lru_cache(maxsize=4)
def get_directory(path: str | None = None) -> TenantDirectory:
    """Return a cached tenant directory."""
    return load_directory(path)
