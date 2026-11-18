"""Runtime configuration, read from environment variables or a local .env file."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dataverse refuses page sizes above 5000, so a larger ceiling would only
# produce a confusing server-side error.
DATAVERSE_PAGE_LIMIT = 5000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATAVERSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = Field(description="Environment URL, e.g. https://yourorg.crm.dynamics.com")
    tenant_id: str
    client_id: str
    client_secret: SecretStr

    allow_writes: bool = False
    default_top: int = Field(default=25, ge=1)
    max_top: int = Field(default=200, ge=1, le=DATAVERSE_PAGE_LIMIT)
    timeout_seconds: float = Field(default=30.0, gt=0)
    api_version: str = "v9.2"

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("DATAVERSE_URL must be an https URL such as https://yourorg.crm.dynamics.com")
        if parts.path not in ("", "/") or parts.query:
            # A path here usually means someone pasted the Web API URL or an app
            # URL; the token audience must be the bare environment origin.
            raise ValueError("DATAVERSE_URL must be the environment root, without a path")
        return f"https://{parts.netloc}"

    @model_validator(mode="after")
    def _check_limits(self) -> Settings:
        if self.default_top > self.max_top:
            raise ValueError("DATAVERSE_DEFAULT_TOP cannot be larger than DATAVERSE_MAX_TOP")
        return self

    @property
    def api_base_url(self) -> str:
        return f"{self.url}/api/data/{self.api_version}/"

    @property
    def scope(self) -> str:
        return f"{self.url}/.default"
