"""Tests for tenant authentication, scopes and the demo tenant."""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from distillserve_gateway.app import create_app
from distillserve_gateway.bootstrap import build_tenants
from distillserve_gateway.core.auth import (
    ROLE_SCOPES,
    Scope,
    Tenant,
    TenantDirectory,
    demo_tenant,
    token_digest,
)
from distillserve_gateway.core.settings import DEFAULT_DEMO_TOKEN

from .conftest import make_settings

DEMO_TOKEN = "demo-token-for-tests"


def build(**overrides: object) -> FastAPI:
    return create_app(make_settings(**overrides))


async def get(app: FastAPI, path: str, token: str | None = None) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            return await http.get(path, headers=headers)


async def post_completion(app: FastAPI, token: str | None = None) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http:
            return await http.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Summarize this."}]},
                headers=headers,
            )


# --- scopes ----------------------------------------------------------------


def test_demo_role_can_read_and_infer_but_not_operate() -> None:
    """The whole point of the demo tenant: usable, harmless."""
    scopes = ROLE_SCOPES["demo"]
    assert Scope.READ in scopes
    assert Scope.INFER in scopes
    assert Scope.OPERATE not in scopes
    assert Scope.ADMIN not in scopes


def test_admin_holds_every_scope() -> None:
    assert ROLE_SCOPES["admin"] == frozenset(Scope)


def test_viewer_cannot_spend_money() -> None:
    assert Scope.INFER not in ROLE_SCOPES["viewer"]


def test_unknown_role_grants_nothing() -> None:
    """Fail closed: a typo in a role name must not become an escalation."""
    tenant = Tenant(id="odd", name="Odd", role="wizard")
    assert tenant.scopes == frozenset()
    assert not tenant.allows(Scope.READ)


# --- token handling --------------------------------------------------------


def test_tokens_are_stored_only_as_digests() -> None:
    tenant = demo_tenant(DEMO_TOKEN)
    assert tenant.token_sha256 == token_digest(DEMO_TOKEN)
    assert DEMO_TOKEN not in tenant.model_dump_json()


def test_directory_authenticates_a_known_token() -> None:
    directory = TenantDirectory(tenants=[demo_tenant(DEMO_TOKEN)])
    assert directory.authenticate(DEMO_TOKEN) is not None
    assert directory.authenticate("wrong") is None


def test_tenants_without_a_token_cannot_authenticate() -> None:
    """The implicit local tenant must not be reachable over HTTP."""
    directory = TenantDirectory(tenants=[Tenant(id="local", name="Local", role="admin")])
    assert directory.authenticate("") is None


# --- bootstrap -------------------------------------------------------------


def test_demo_access_is_on_by_default() -> None:
    """A fresh deployment is demonstrable with no setup — that is the point."""
    directory = build_tenants(make_settings())
    demo = directory.by_id("demo")

    assert demo is not None
    assert demo.role == "demo"
    assert directory.authenticate(DEFAULT_DEMO_TOKEN) is not None


def test_demo_access_can_be_disabled() -> None:
    """An empty token means no demo tenant at all, not a tenant with no token."""
    directory = build_tenants(make_settings(DISTILLSERVE_DEMO_TOKEN=""))
    assert directory.by_id("demo") is None


def test_demo_tenant_is_added_when_a_token_is_configured() -> None:
    directory = build_tenants(make_settings(DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN))
    demo = directory.by_id("demo")

    assert demo is not None
    assert demo.role == "demo"
    assert demo.requests_per_minute == 20
    assert demo.monthly_budget_usd == 5.0


def test_demo_rate_cap_is_configurable() -> None:
    directory = build_tenants(
        make_settings(DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN, DISTILLSERVE_DEMO_RPM=5)
    )
    demo = directory.by_id("demo")
    assert demo is not None
    assert demo.requests_per_minute == 5


# --- HTTP behaviour --------------------------------------------------------


async def test_local_development_runs_as_the_local_admin_tenant() -> None:
    response = await get(build(), "/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "local"
    assert body["authenticated"] is False
    assert "operate" in body["scopes"]


async def test_demo_token_identifies_the_demo_tenant() -> None:
    app = build(DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN, DISTILLSERVE_AUTH_REQUIRED=True)
    response = await get(app, "/v1/me", token=DEMO_TOKEN)

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "demo"
    assert body["scopes"] == ["infer", "read"]
    assert body["authenticated"] is True


async def test_identity_never_echoes_the_token() -> None:
    app = build(DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN)
    response = await get(app, "/v1/me", token=DEMO_TOKEN)

    assert DEMO_TOKEN not in response.text


async def test_missing_token_is_rejected_when_auth_is_required() -> None:
    app = build(DISTILLSERVE_AUTH_REQUIRED=True, DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN)
    response = await get(app, "/v1/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("auth_required", [True, False])
async def test_a_bad_token_is_always_rejected(auth_required: bool) -> None:
    """Downgrading an unrecognised credential to admin would be a trap."""
    app = build(DISTILLSERVE_AUTH_REQUIRED=auth_required, DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN)
    response = await get(app, "/v1/me", token="not-the-token")

    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "invalid_token_error"


async def test_health_endpoints_stay_unauthenticated() -> None:
    """A load balancer has no credentials; requiring one would fail every probe."""
    app = build(DISTILLSERVE_AUTH_REQUIRED=True, DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN)

    assert (await get(app, "/healthz")).status_code == 200
    assert (await get(app, "/readyz")).status_code in {200, 503}


async def test_completions_require_the_infer_scope() -> None:
    app = build(DISTILLSERVE_AUTH_REQUIRED=True, DISTILLSERVE_DEMO_TOKEN=DEMO_TOKEN)
    response = await post_completion(app)

    assert response.status_code == 401
