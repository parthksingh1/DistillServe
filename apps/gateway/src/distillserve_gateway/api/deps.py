"""Request-scoped dependencies: the authenticated tenant and scope checks.

Kept separate from the routers so that every endpoint added in later phases —
rollouts, adapters, the registry — declares its required scope the same way,
with one import and one annotation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from distillserve_gateway.core.auth import LOCAL_TENANT, Scope, Tenant, TenantDirectory
from distillserve_otel import get_logger
from distillserve_schemas import GatewayError, GatewayErrorBody

log = get_logger(__name__)

_BEARER = "bearer "


def _unauthorized(message: str, error_type: str) -> HTTPException:
    """Build a 401 whose body matches the gateway's error envelope."""
    body = GatewayError(error=GatewayErrorBody(message=message, type=error_type))
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=body.model_dump(mode="json"),
        # Tells a browser client which scheme to use; omitting it makes a 401
        # indistinguishable from a generic denial.
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_token(request: Request) -> str | None:
    """Pull the bearer token out of the Authorization header."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith(_BEARER):
        return None
    token = header[len(_BEARER) :].strip()
    return token or None


def current_tenant(request: Request) -> Tenant:
    """Resolve the tenant making this request.

    When authentication is disabled — the default for local development — the
    request runs as the built-in ``local`` admin tenant rather than as an
    anonymous caller, so downstream code never has to handle "no tenant".

    Raises:
        HTTPException: 401 when auth is required and the token is missing or
            unrecognised.
    """
    directory: TenantDirectory = request.app.state.tenants
    settings = request.app.state.settings

    token = _extract_token(request)
    if token is not None:
        tenant = directory.authenticate(token)
        if tenant is not None:
            return tenant
        # An unrecognised token is always rejected, even when auth is optional:
        # silently downgrading a bad credential to admin would be a trap.
        log.warning("auth.token_rejected", path=request.url.path)
        raise _unauthorized("The supplied bearer token is not recognised.", "invalid_token_error")

    if settings.auth_required:
        raise _unauthorized(
            "This endpoint requires a bearer token. Use the demo token to explore the platform.",
            "authentication_error",
        )
    return LOCAL_TENANT


TenantDep = Annotated[Tenant, Depends(current_tenant)]


def require_scope(scope: Scope) -> Callable[[Tenant], Tenant]:
    """Build a dependency that enforces ``scope``.

    Routes ask for a scope, never for a role. Introducing a new role later is
    then a configuration change rather than an edit to every endpoint.
    """

    def _dependency(tenant: TenantDep) -> Tenant:
        if not tenant.allows(scope):
            body = GatewayError(
                error=GatewayErrorBody(
                    message=(
                        f"Tenant '{tenant.id}' (role '{tenant.role}') lacks the "
                        f"'{scope.value}' scope required for this operation."
                    ),
                    type="insufficient_scope_error",
                )
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=body.model_dump(mode="json")
            )
        return tenant

    return _dependency


#: Ready-made dependencies for the two scopes the gateway uses today.
RequireInfer = Annotated[Tenant, Depends(require_scope(Scope.INFER))]
RequireRead = Annotated[Tenant, Depends(require_scope(Scope.READ))]
RequireOperate = Annotated[Tenant, Depends(require_scope(Scope.OPERATE))]
