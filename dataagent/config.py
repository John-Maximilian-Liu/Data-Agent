"""Validated environment configuration. Secrets must never appear in logs or responses."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATAAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent",
        description="SQLAlchemy URL (psycopg3)",
    )
    auth_mode: Literal["oidc", "offline"] = "offline"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    environment: Literal["development", "test", "production"] = "development"

    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: SecretStr | None = None
    azure_use_entra: bool = True
    azure_transport: Literal["live", "fake"] = "fake"

    default_timezone: str = "UTC"
    hmac_pepper: SecretStr = SecretStr("dev-only-pepper-change-me")
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    worker_id: str = "worker-local"
    job_lease_seconds: int = 60
    max_ingest_batch: int = 500
    synthetic_agent_count: int = 1000

    @field_validator("database_url")
    @classmethod
    def _require_postgres(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must use PostgreSQL (postgresql+psycopg://...)")
        return v

    @model_validator(mode="after")
    def _reject_offline_in_production(self) -> Settings:
        if self.environment == "production" and self.auth_mode == "offline":
            raise ValueError("offline auth is rejected in production")
        return self

    def redacted_dict(self) -> dict[str, object]:
        data = self.model_dump()
        for key in ("azure_openai_api_key", "hmac_pepper"):
            if key in data and data[key] is not None:
                data[key] = "***"
        return data


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
