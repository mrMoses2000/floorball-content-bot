from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    floorball_site_repo: Path = Field(
        default=Path("/home/moses/floorball.kz"), validation_alias="FLOORBALL_SITE_REPO"
    )
    media_root: Path = Field(default=Path("./var/media"), validation_alias="MEDIA_ROOT")
    worktree_root: Path = Field(default=Path("./var/worktrees"), validation_alias="WORKTREE_ROOT")
    backup_root: Path = Field(default=Path("./var/backups"), validation_alias="BACKUP_ROOT")
    codex_cli: str = Field(default="codex", validation_alias="CODEX_CLI")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    max_download_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_derivative_bytes: int = 2 * 1024 * 1024
    codex_timeout_seconds: int = 120
    assemblyai_timeout_seconds: int = 300
    job_lease_seconds: int = 300
    max_job_attempts: int = 5
    poll_timeout_seconds: int = 30
    run_external_tests: bool = Field(default=False, validation_alias="RUN_EXTERNAL_TESTS")

    @field_validator("media_root", "worktree_root", "backup_root", mode="after")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
