import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration supplied by the deployment environment."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    app_name: str = "LaValienteService"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"

    #: Whether `/docs`, `/redoc` and `/openapi.json` are published. `None` means
    #: "everywhere except production": the interactive documentation is the whole
    #: map of the API — every route, every field a body accepts, every permission
    #: it enforces — which is a working tool while building and free
    #: reconnaissance once the URL is public. Set it to `true` to bring it back
    #: for an afternoon without shipping code.
    docs_enabled: bool | None = None
    database_url: PostgresDsn
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "lavaliente-api"
    jwt_audience: str = "lavaliente-app"
    jwt_leeway_seconds: int = Field(default=10, ge=0, le=60)
    access_token_minutes: int = Field(default=15, gt=0, le=60)
    refresh_token_days: int = Field(default=7, gt=0, le=30)
    session_absolute_days: int = Field(default=30, gt=0, le=90)
    refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=120)
    reset_code_minutes: int = Field(default=10, gt=0, le=60)
    reset_code_max_attempts: int = Field(default=5, gt=0, le=10)
    notifications_backend: str = "console"

    #: Origins allowed to call this API *from a browser*. Empty by default and
    #: that is the right answer today: the Flutter app is not a browser and no
    #: same-origin rule applies to it. Empty means no CORS headers at all, which
    #: is stricter than sending permissive ones "just in case"; the day there is
    #: a web screen, this variable is what turns it on.
    cors_origins: Annotated[list[str], NoDecode] = []

    #: Where product images live (Plan 0005 §8.4, D10). Only the path reaches the
    #: database; the bytes stay on disk today and can move to object storage
    #: tomorrow without touching the schema.
    media_dir: Path = Path("./media")
    media_max_image_mb: int = Field(default=5, gt=0, le=50)
    media_allowed_formats: Annotated[list[str], NoDecode] = ["jpg", "jpeg", "png", "webp"]

    # --- Escaneo de boleta con IA (Plan 0003 §8). Todo el bloque es del módulo
    # `intake_scan`, que es temporal por diseño (D2): el día del retiro estos
    # ajustes se borran con él y nada más los lee.
    #: Interruptor global (D2). Apagado por omisión: un despliegue sin key no
    #: debe ofrecer un botón que sólo puede fallar.
    scan_enabled: bool = False
    #: Secreto, y sólo del backend (D1). La app nunca lo ve: sube la foto a
    #: nuestra API y es el servidor quien habla con Gemini.
    gemini_api_key: SecretStr | None = None
    scan_model: str = "gemini-2.5-flash"
    #: Debe existir como `prompts/{version}.md` (D5).
    scan_prompt_version: str = "v1"
    #: El de la hoja «Registro Diario», que es otro documento y por tanto otro
    #: prompt con su propia historia. Versionado aparte a propósito: mejorar la
    #: lectura de la boleta no debe obligar a revalidar la del cierre, ni al
    #: revés.
    scan_cash_prompt_version: str = "cash_v1"
    scan_timeout_s: int = Field(default=30, gt=0, le=120)
    scan_max_image_mb: int = Field(default=8, gt=0, le=50)
    #: Tope diario de escaneos, que es el control de costos del §12.2.
    scan_daily_limit: int = Field(default=200, ge=0)
    scan_storage_path: Path = Path("./media/scans")
    scan_image_retention_days: int = Field(default=90, ge=1)

    @field_validator("media_allowed_formats", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept `jpg,png` from the environment, not only a JSON array.

        `MEDIA_ALLOWED_FORMATS=jpg,jpeg,png,webp` is how §8.4 writes it and how
        anyone would write it in a `.env`; without this, pydantic would demand
        `["jpg", ...]` and fail the whole settings load over a comma.

        The `NoDecode` on both fields is what lets this validator see the string
        at all. By default pydantic-settings decodes every complex type as JSON
        *inside the environment source*, before field validation runs, so a
        comma-separated value did not reach here to be split: it raised on
        `json.loads` and took the whole process down at startup — which is the
        worst place to learn that a variable is written with commas.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            decoded: object = json.loads(text)
            return decoded
        return [item.strip() for item in text.split(",") if item.strip()]

    @property
    def media_max_image_bytes(self) -> int:
        return self.media_max_image_mb * 1024 * 1024

    @property
    def scan_max_image_bytes(self) -> int:
        return self.scan_max_image_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def serve_docs(self) -> bool:
        if self.docs_enabled is None:
            return not self.is_production
        return self.docs_enabled

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
