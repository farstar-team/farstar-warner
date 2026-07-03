from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: SecretStr
    admin_telegram_id: int = Field(gt=0)

    postgres_db: str = "farstar_warner"
    postgres_user: str = "farstar"
    postgres_password: SecretStr
    postgres_host: str = "postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    database_pool_size: int = Field(default=10, ge=2, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)

    redis_host: str = "redis"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr

    check_interval_seconds: int = Field(default=300, ge=30, le=86400)
    check_concurrency: int = Field(default=8, ge=1, le=50)
    deactivation_confirmations: int = Field(default=2, ge=1, le=5)
    deactivation_confirmation_delay_seconds: float = Field(
        default=15.0, ge=3.0, le=120.0
    )
    check_jitter_min_seconds: float = Field(default=0.5, ge=0, le=30)
    check_jitter_max_seconds: float = Field(default=3.0, ge=0, le=60)
    instagram_base_url: str = "https://www.instagram.com"
    instagram_proxy_url: str | None = None
    instagram_search_doc_id: str = "26347858941511777"
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_graph_api_version: str = "v21.0"
    credential_encryption_key: SecretStr | None = None
    instagram_request_timeout_seconds: float = Field(default=20.0, ge=5, le=60)
    proxy_health_url: str = "https://www.cloudflare.com/cdn-cgi/trace"
    page_check_delay_min_seconds: float = Field(default=15.0, ge=1, le=60)
    page_check_delay_max_seconds: float = Field(default=45.0, ge=1, le=120)
    rate_limit_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    chromium_executable: str = "/usr/bin/chromium"
    profile_preview_timeout_seconds: int = Field(default=45, ge=10, le=60)
    profile_preview_cache_seconds: int = Field(default=900, ge=60, le=86400)
    profile_preview_concurrency: int = Field(default=2, ge=1, le=5)
    free_trial_days: int = Field(default=7, ge=1, le=365)
    log_level: str = "INFO"

    @field_validator("postgres_db", "postgres_user")
    @classmethod
    def validate_database_identifier(cls, value: str) -> str:
        if not value.replace("_", "a").isalnum() or not (
            value[0].isalpha() or value[0] == "_"
        ):
            raise ValueError(
                "database identifiers may contain only letters, numbers, and underscores"
            )
        return value

    @field_validator("instagram_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("instagram_base_url must use HTTPS")
        return value

    @field_validator("instagram_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("socks5://", "http://", "https://")):
            raise ValueError("instagram_proxy_url must use socks5, HTTP, or HTTPS")
        return normalized

    @field_validator("instagram_search_doc_id")
    @classmethod
    def validate_instagram_search_doc_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit() or len(normalized) > 32:
            raise ValueError("instagram_search_doc_id must be numeric")
        return normalized

    @field_validator("proxy_health_url")
    @classmethod
    def validate_proxy_health_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("https://"):
            raise ValueError("proxy_health_url must use HTTPS")
        return normalized

    @field_validator("meta_graph_base_url")
    @classmethod
    def normalize_meta_graph_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if normalized != "https://graph.facebook.com":
            raise ValueError("meta_graph_base_url must be https://graph.facebook.com")
        return normalized

    @field_validator("meta_graph_api_version")
    @classmethod
    def validate_meta_graph_api_version(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.startswith("v"):
            normalized = f"v{normalized}"
        parts = normalized[1:].split(".")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("meta_graph_api_version must look like v21.0")
        return normalized

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @model_validator(mode="after")
    def validate_jitter_range(self) -> Settings:
        if self.check_jitter_min_seconds > self.check_jitter_max_seconds:
            raise ValueError(
                "check_jitter_min_seconds cannot exceed check_jitter_max_seconds"
            )
        if self.page_check_delay_min_seconds > self.page_check_delay_max_seconds:
            raise ValueError(
                "page_check_delay_min_seconds cannot exceed "
                "page_check_delay_max_seconds"
            )
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg_async",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def redis_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return (
            f"redis://:{password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
