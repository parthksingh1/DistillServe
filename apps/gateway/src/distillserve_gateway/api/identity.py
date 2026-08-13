"""Who am I: the identity endpoint the console uses after sign-in.

The admin console has no user database of its own. It presents a token, calls
this endpoint, and renders whatever scopes come back — which is what lets the
same build serve an operator and a demo visitor with different affordances
rather than different code.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from distillserve_gateway.api.deps import TenantDep

router = APIRouter(prefix="/v1", tags=["identity"])


class IdentityResponse(BaseModel):
    """The calling tenant, as the console needs to see it.

    The token is never echoed, in any form — not even a prefix. A response body
    ends up in browser devtools, screenshots and support tickets.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    name: str
    role: str
    scopes: list[str] = Field(description="Sorted scope names granted to this tenant.")
    requests_per_minute: int
    monthly_budget_usd: float | None = None
    authenticated: bool = Field(
        description="False when running as the implicit local tenant with auth disabled."
    )


@router.get("/me", response_model=IdentityResponse, summary="Describe the calling tenant")
async def whoami(tenant: TenantDep) -> IdentityResponse:
    """Return the tenant's identity, role and scopes."""
    return IdentityResponse(
        tenant_id=tenant.id,
        name=tenant.name,
        role=tenant.role,
        scopes=sorted(scope.value for scope in tenant.scopes),
        requests_per_minute=tenant.requests_per_minute,
        monthly_budget_usd=tenant.monthly_budget_usd,
        authenticated=tenant.token_sha256 is not None,
    )
