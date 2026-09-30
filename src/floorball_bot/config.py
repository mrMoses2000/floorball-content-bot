from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    telegram_token: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("TG_API_KEY", "TELEGRAM_BOT_TOKEN")
    )
    assemblyai_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("ASSEMBLI_AI", "ASSEMBLYAI_API_KEY"),
    )
    postgres_dsn: SecretStr = Field(
        default=SecretStr("postgresql://floorballbot@127.0.0.1:5432/floorball_bot"),
        validation_alias="POSTGRES_DSN",
    )
    db_command_timeout_seconds: int = Field(
        default=60, validation_alias="DB_COMMAND_TIMEOUT_SECONDS"
    )
    db_acquire_timeout_seconds: int = Field(
        default=30, validation_alias="DB_ACQUIRE_TIMEOUT_SECONDS"
    )
    db_pool_min_size: int = Field(default=1, validation_alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=5, validation_alias="DB_POOL_MAX_SIZE")
    floorball_site_repo: Path = Field(
        default=Path("/home/moses/floorball.kz"), validation_alias="FLOORBALL_SITE_REPO"
    )
    media_root: Path = Field(default=Path("./var/media"), validation_alias="MEDIA_ROOT")
    worktree_root: Path = Field(default=Path("./var/worktrees"), validation_alias="WORKTREE_ROOT")
    backup_root: Path = Field(default=Path("./var/backups"), validation_alias="BACKUP_ROOT")
    agy_cli: str = Field(default="agy", validation_alias="AGY_CLI")
    agy_model: str = Field(default="gemini-3.8-flash", validation_alias="AGY_MODEL")
    agy_effort: Literal["low", "medium", "high"] = Field(
        default="high", validation_alias="AGY_EFFORT"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    max_download_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_derivative_bytes: int = 2 * 1024 * 1024
    agy_timeout_seconds: int = Field(default=180, validation_alias="AGY_TIMEOUT_SECONDS")
    assemblyai_timeout_seconds: int = 300
    job_lease_seconds: int = 300
    max_job_attempts: int = 5
    poll_timeout_seconds: int = 30
    run_external_tests: bool = Field(default=False, validation_alias="RUN_EXTERNAL_TESTS")
    publish_enabled: bool = Field(default=False, validation_alias="PUBLISH_ENABLED")
    plesk_static_webhook_url: SecretStr = Field(
        default=SecretStr(""), validation_alias="PLESK_STATIC_WEBHOOK_URL"
    )
    plesk_source_webhook_url: SecretStr = Field(
        default=SecretStr(""), validation_alias="PLESK_SOURCE_WEBHOOK_URL"
    )
    mini_app_public_url: str = Field(default="", validation_alias="MINI_APP_PUBLIC_URL")
    mini_app_host: str = Field(default="127.0.0.1", validation_alias="MINI_APP_HOST")
    mini_app_port: int = Field(default=8092, validation_alias="MINI_APP_PORT")
    mini_app_auth_max_age_seconds: int = Field(
        default=86_400, validation_alias="MINI_APP_AUTH_MAX_AGE_SECONDS"
    )
    mini_app_dist_root: Path = Field(
        default=Path("./miniapp/dist"), validation_alias="MINI_APP_DIST_ROOT"
    )

    @field_validator(
        "media_root", "worktree_root", "backup_root", "mini_app_dist_root", mode="after"
    )
    @classmethod
    def absolute_runtime_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    def require_telegram_token(self) -> str:
        value = self.telegram_token.get_secret_value().strip()
        if not value:
            raise RuntimeError("TG_API_KEY is not configured")
        return value

    def require_assemblyai_key(self) -> str:
        value = self.assemblyai_key.get_secret_value().strip()
        if not value:
            raise RuntimeError("ASSEMBLI_AI/ASSEMBLYAI_API_KEY is not configured")
        return value

    @field_validator("mini_app_public_url", mode="after")
    @classmethod
    def valid_mini_app_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("MINI_APP_PUBLIC_URL must use HTTPS")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
