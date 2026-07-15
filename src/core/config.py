from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration supplied by the deployment environment."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    app_name: str = "LaValienteService"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"
    database_url: PostgresDsn
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = Field(default=15, gt=0, le=60)
    refresh_token_days: int = Field(default=7, gt=0, le=30)
    cors_origins: list[str] = []

    @property
    def async_database_url(self) -> str:
        url = str(self.database_url).replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        return self.async_database_url.replace("postgresql+asyncpg", "postgresql+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
