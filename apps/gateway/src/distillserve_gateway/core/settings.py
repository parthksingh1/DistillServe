"""Gateway configuration.

One ``Settings`` object is the single source of truth for every knob the
gateway has. Two rules keep it honest:

1. **Secrets are ``SecretStr``.** They cannot be logged by accident — repr and
   ``model_dump()`` both redact them, and the value is only reachable through
   an explicit ``.get_secret_value()``.
2. **Mode invariants are validated at startup, not at request time.** A
   ``self_hosted`` deployment without ``VLLM_ENDPOINT`` fails the process
   immediately rather than 500-ing on the first request an hour later.

Env var names are unprefixed where they are conventional for the vendor
(``GROQ_API_KEY``, ``REDIS_URL``) and ``DISTILLSERVE_``-prefixed for everything
this platform owns.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from distillserve_schemas import DeploymentMode, RuntimeEnvironment

#: Teacher/student pairs per provider. The teacher is the frontier model the
#: student is distilled from and evaluated against; the student is the small,
#: fast model that serves the tasks it has parity on.
DEFAULT_MODEL_PAIRS: dict[str, tuple[str, str]] = {
    "groq": ("groq/llama-3.3-70b-versatile", "groq/llama-3.1-8b-instant"),
    "gemini": ("gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash-lite"),
    "openai": ("openai/gpt-4o", "openai/gpt-4o-mini"),
    "anthropic": ("anthropic/claude-sonnet-4-5", "anthropic/claude-haiku-4-5"),
}

#: Used when no provider key is configured at all — reachable only in `test`
#: and `self_hosted`, where generation does not go through a hosted provider.
_FALLBACK_PROVIDER = "groq"

#: The published demo credential.
#:
#: This is deliberately *not* a secret. The demo tenant holds `read` and
#: `infer` only, is rate-capped and budget-capped, and cannot change any
#: platform state — so publishing the token costs nothing and means a fresh
#: deployment can be handed to someone with no setup at all. Any deployment
#: that wants private demo access overrides DISTILLSERVE_DEMO_TOKEN.
DEFAULT_DEMO_TOKEN = "distillserve-demo"  # noqa: S105 - a published, read-only credential


class Settings(BaseSettings):
    """Runtime configuration for the gateway process."""

    model_config = SettingsConfigDict(
        env_file=(".env", "apps/gateway/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Identity ----------------------------------------------------------
    service_name: str = Field(default="distillserve-gateway", alias="DISTILLSERVE_SERVICE_NAME")
    mode: DeploymentMode = Field(default=DeploymentMode.HOSTED, alias="DISTILLSERVE_MODE")
    environment: RuntimeEnvironment = Field(
        default=RuntimeEnvironment.LOCAL, alias="DISTILLSERVE_ENV"
    )

    # --- HTTP --------------------------------------------------------------
    host: str = Field(default="0.0.0.0", alias="DISTILLSERVE_HOST")  # noqa: S104
    port: int = Field(default=8000, ge=1, le=65_535, alias="PORT")
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"],
        alias="DISTILLSERVE_CORS_ALLOW_ORIGINS",
        description="Comma-separated list of browser origins allowed to call the gateway.",
    )

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(default="info", alias="DISTILLSERVE_LOG_LEVEL")
    log_json: bool = Field(default=True, alias="DISTILLSERVE_LOG_JSON")

    # --- Providers (hosted + sandbox generation path) ----------------------
    groq_api_key: SecretStr | None = Field(default=None, alias="GROQ_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Empty by default and derived from whichever provider is configured. An
    # explicit setting always wins; the derivation exists so that a single API
    # key is enough to get a working teacher/student pair without the operator
    # having to look up each provider's model ids.
    teacher_model: str = Field(default="", alias="DISTILLSERVE_TEACHER_MODEL")
    student_model: str = Field(default="", alias="DISTILLSERVE_STUDENT_MODEL")

    # --- Self-hosted serving ----------------------------------------------
    vllm_endpoint: str | None = Field(
        default=None,
        alias="VLLM_ENDPOINT",
        description="Base URL of the vLLM OpenAI-compatible server, e.g. http://vllm:8000/v1.",
    )

    # --- Platform state ----------------------------------------------------
    platform_db_path: str = Field(
        default="var/distillserve.sqlite",
        alias="DISTILLSERVE_DB_PATH",
        description="SQLite file holding the model registry, adapters and eval reports.",
    )
    audit_db_path: str = Field(
        default="var/audit.sqlite",
        alias="DISTILLSERVE_AUDIT_DB_PATH",
        description="SQLite file holding the hash-chained rollout audit log.",
    )
    audit_secret: SecretStr | None = Field(
        default=None,
        alias="DISTILLSERVE_AUDIT_SECRET",
        description="HMAC key for the audit chain. Without it entries still chain "
        "(so completeness is verifiable) but cannot prove authorship.",
    )

    # --- Guardrails --------------------------------------------------------
    injection_defense_enabled: bool = Field(default=True, alias="DISTILLSERVE_INJECTION_DEFENSE")
    injection_block_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, alias="DISTILLSERVE_INJECTION_BLOCK_THRESHOLD"
    )
    injection_flag_threshold: float = Field(
        default=0.3, ge=0.0, le=1.0, alias="DISTILLSERVE_INJECTION_FLAG_THRESHOLD"
    )
    pii_redaction_enabled: bool = Field(default=True, alias="DISTILLSERVE_PII_REDACTION")

    # --- Semantic cache ----------------------------------------------------
    cache_enabled: bool = Field(default=True, alias="DISTILLSERVE_CACHE_ENABLED")
    cache_similarity_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        alias="DISTILLSERVE_CACHE_THRESHOLD",
        description="High by design: a false hit answers a different question.",
    )
    cache_ttl_seconds: int = Field(default=3600, gt=0, alias="DISTILLSERVE_CACHE_TTL")
    embedding_model: str | None = Field(
        default=None,
        alias="DISTILLSERVE_EMBEDDING_MODEL",
        description="LiteLLM embedding model id for the semantic cache, e.g. "
        "gemini/text-embedding-004. Unset falls back to a dependency-free lexical "
        "embedder that catches near-duplicates but not paraphrases.",
    )

    # --- Rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = Field(default=True, alias="DISTILLSERVE_RATE_LIMIT")
    default_requests_per_minute: int = Field(default=60, gt=0, alias="DISTILLSERVE_DEFAULT_RPM")

    # --- Access control ----------------------------------------------------
    auth_required: bool = Field(
        default=False,
        alias="DISTILLSERVE_AUTH_REQUIRED",
        description="Reject unauthenticated requests. Off locally, on in production.",
    )
    tenants_file: str | None = Field(
        default=None,
        alias="DISTILLSERVE_TENANTS_FILE",
        description="YAML file of tenants with SHA-256 token digests.",
    )
    demo_token: SecretStr | None = Field(
        default=SecretStr(DEFAULT_DEMO_TOKEN),
        alias="DISTILLSERVE_DEMO_TOKEN",
        description="Bearer token for the built-in read-and-infer demo tenant. "
        "Defaults to a published, well-known value so a fresh deployment is "
        "demonstrable immediately; override it with your own for anything you "
        "care about. Set it empty to disable demo access entirely.",
    )
    demo_requests_per_minute: int = Field(
        default=20,
        gt=0,
        alias="DISTILLSERVE_DEMO_RPM",
        description="Rate cap for the demo tenant, so a shared link cannot exhaust quota.",
    )

    # --- Infrastructure ----------------------------------------------------
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    # --- Observability -----------------------------------------------------
    langfuse_public_key: SecretStr | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: SecretStr | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0, alias="OTEL_SAMPLE_RATIO")

    # --- Reference dataset (sandbox telemetry source) ----------------------
    reference_data_dir: str = Field(
        default="data/reference", alias="DISTILLSERVE_REFERENCE_DATA_DIR"
    )
    reference_seed: int = Field(
        default=20250901,
        alias="DISTILLSERVE_REFERENCE_SEED",
        description="Seed making sandbox telemetry reproducible across restarts.",
    )

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept a comma-separated string, because env vars have no list type."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        """Lower-case the level so ``INFO`` and ``info`` behave identically."""
        allowed = {"critical", "error", "warning", "info", "debug"}
        level = value.strip().lower()
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @model_validator(mode="after")
    def _derive_default_models(self) -> Self:
        """Fill unset teacher/student ids from the first configured provider.

        Both come from the *same* provider on purpose: the pair is compared
        head-to-head on every eval surface, and mixing vendors would confound
        the distillation delta with a vendor difference.
        """
        provider = next(iter(self.configured_providers), _FALLBACK_PROVIDER)
        teacher, student = DEFAULT_MODEL_PAIRS[provider]
        if not self.teacher_model:
            self.teacher_model = teacher
        if not self.student_model:
            self.student_model = student
        return self

    @model_validator(mode="after")
    def _check_mode_invariants(self) -> Self:
        """Fail fast when the selected mode is missing what it needs to serve.

        `hosted` and `sandbox` both generate through hosted providers, so both
        need at least one provider key. `self_hosted` needs a vLLM endpoint.
        """
        if self.mode is DeploymentMode.SELF_HOSTED and not self.vllm_endpoint:
            raise ValueError("DISTILLSERVE_MODE=self_hosted requires VLLM_ENDPOINT to be set.")
        # Tests and CI run without provider keys; every other environment must
        # have one, because both hosted and sandbox generate through a provider.
        needs_provider_key = (
            self.mode is not DeploymentMode.SELF_HOSTED
            and self.environment is not RuntimeEnvironment.TEST
        )
        if needs_provider_key and not self.has_any_provider_key:
            raise ValueError(
                f"DISTILLSERVE_MODE={self.mode.value} requires at least one provider key "
                "(GROQ_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY)."
            )
        return self

    @property
    def configured_providers(self) -> tuple[str, ...]:
        """Providers with a key, in the order the model derivation prefers them."""
        keys = {
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }
        return tuple(name for name, secret in keys.items() if secret is not None)

    @property
    def has_any_provider_key(self) -> bool:
        """True when at least one hosted provider is configured."""
        return bool(self.configured_providers)

    @property
    def langfuse_enabled(self) -> bool:
        """True when both Langfuse keys are present, so spans can be exported."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def otlp_traces_endpoint(self) -> str:
        """Langfuse's OTLP/HTTP traces endpoint derived from ``langfuse_host``."""
        return f"{self.langfuse_host.rstrip('/')}/api/public/otel/v1/traces"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that env parsing and validation happen once. Tests clear the
    cache via ``get_settings.cache_clear()`` after patching the environment.
    """
    return Settings()
