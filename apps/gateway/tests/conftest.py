"""Shared fixtures for gateway tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from distillserve_gateway.app import create_app
from distillserve_gateway.core.settings import Settings, get_settings
from distillserve_schemas import DeploymentMode, RuntimeEnvironment


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the settings singleton from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_settings(**overrides: object) -> Settings:
    """Build test settings without reading the developer's real environment."""
    base: dict[str, object] = {
        "DISTILLSERVE_MODE": DeploymentMode.HOSTED,
        "DISTILLSERVE_ENV": RuntimeEnvironment.TEST,
        "DISTILLSERVE_LOG_JSON": True,
        "GROQ_API_KEY": "test-groq-key",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def build_client(settings: Settings) -> tuple[object, AsyncClient]:
    """Return an app and a client bound to it, for tests needing custom settings."""
    app = create_app(settings)
    transport = ASGITransport(app=app)
    return app, AsyncClient(transport=transport, base_url="http://gateway.test")


@pytest.fixture
def settings() -> Settings:
    """Default hosted-mode test settings."""
    return make_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """An httpx client bound to the app, with lifespan startup/shutdown run."""
    app, http = build_client(settings)
    async with LifespanManager(app), http:  # type: ignore[arg-type]
        yield http
