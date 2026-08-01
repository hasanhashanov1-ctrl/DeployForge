from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DEPLOYFORGE_",
        extra="ignore",
    )

    app_name: str = "DeployForge"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://deployforge:deployforge@postgres/deployforge"
    sync_database_url: str = "postgresql+psycopg://deployforge:deployforge@postgres/deployforge"
    redis_url: str = "redis://redis:6379/0"
    secret_key: SecretStr = Field(
        default=SecretStr("deployforge-local-development-key-change-me"), min_length=16
    )
    runtime_network: str = "deployforge-runtime"
    http_port: int = Field(default=80, ge=1, le=65535)
    clone_timeout_seconds: int = Field(default=120, ge=10, le=900)
    startup_timeout_seconds: int = Field(default=60, ge=3, le=300)
    startup_grace_seconds: int = Field(default=3, ge=1, le=30)
    build_log_limit_bytes: int = Field(default=1_048_576, ge=1024)
    runtime_log_limit_bytes: int = Field(default=1_048_576, ge=1024)
    runtime_log_tail: int = Field(default=1000, ge=10, le=10_000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
