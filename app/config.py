from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "thai-2d-api"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./thai_2d.db"
    admin_api_key: str = Field(default="change-me", min_length=8)

    collector_enabled: bool = True
    collector_interval_seconds: int = Field(default=60, ge=10)
    stale_after_seconds: int = Field(default=300, ge=1)

    set_json_url: str = "https://www.set.or.th/api/set/index/SET/overview"
    set_page_url: str = "https://www.set.or.th/en/market/index/set/overview"
    set_request_timeout_seconds: float = Field(default=15.0, gt=0)
    set_max_retries: int = Field(default=3, ge=1, le=10)
    set_user_agent: str = "thai-2d-api/1.0 (public market-data collector)"
    playwright_fallback_enabled: bool = False
    playwright_headless: bool = True

    two_d_strategy: str = "raw_only"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
