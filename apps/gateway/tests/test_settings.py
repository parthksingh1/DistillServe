"""Tests for configuration parsing and the per-mode startup invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from distillserve_gateway.core.settings import Settings
from distillserve_schemas import DeploymentMode, RuntimeEnvironment


def test_defaults_to_hosted_mode() -> None:
    settings = Settings.model_validate({"DISTILLSERVE_ENV": RuntimeEnvironment.TEST})
    assert settings.mode is DeploymentMode.HOSTED


def test_self_hosted_requires_vllm_endpoint() -> None:
    with pytest.raises(ValidationError, match="requires VLLM_ENDPOINT"):
        Settings.model_validate({"DISTILLSERVE_MODE": "self_hosted", "DISTILLSERVE_ENV": "test"})


def test_non_test_environment_requires_a_provider_key() -> None:
    with pytest.raises(ValidationError, match="requires at least one provider key"):
        Settings.model_validate({"DISTILLSERVE_MODE": "sandbox", "DISTILLSERVE_ENV": "production"})


def test_cors_origins_accept_a_comma_separated_string() -> None:
    settings = Settings.model_validate(
        {
            "DISTILLSERVE_ENV": "test",
            "DISTILLSERVE_CORS_ALLOW_ORIGINS": "https://a.example, https://b.example",
        }
    )
    assert settings.cors_allow_origins == ["https://a.example", "https://b.example"]


def test_secrets_are_not_exposed_by_repr() -> None:
    settings = Settings.model_validate(
        {"DISTILLSERVE_ENV": "test", "GROQ_API_KEY": "super-secret-value"}
    )
    assert "super-secret-value" not in repr(settings)
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == "super-secret-value"


def test_otlp_endpoint_is_derived_from_langfuse_host() -> None:
    settings = Settings.model_validate(
        {"DISTILLSERVE_ENV": "test", "LANGFUSE_HOST": "https://eu.cloud.langfuse.com/"}
    )
    assert (
        settings.otlp_traces_endpoint == "https://eu.cloud.langfuse.com/api/public/otel/v1/traces"
    )


def test_langfuse_export_needs_both_keys() -> None:
    only_public = Settings.model_validate(
        {"DISTILLSERVE_ENV": "test", "LANGFUSE_PUBLIC_KEY": "pk-lf-1"}
    )
    both = Settings.model_validate(
        {
            "DISTILLSERVE_ENV": "test",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
            "LANGFUSE_SECRET_KEY": "sk-lf-1",
        }
    )
    assert only_public.langfuse_enabled is False
    assert both.langfuse_enabled is True


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="log_level must be one of"):
        Settings.model_validate({"DISTILLSERVE_ENV": "test", "DISTILLSERVE_LOG_LEVEL": "loud"})
