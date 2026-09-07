"""Task 4 settings: provider endpoints, on-disk quota store and deadlines."""

import os
from pathlib import Path

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

DEFAULT_PRIMARY_URL = "http://127.0.0.1:9002/primary/completions"
DEFAULT_SECONDARY_URL = "http://127.0.0.1:9002/secondary/completions"
DEFAULT_DATABASE_PATH = "./var/rate_limit.sqlite3"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    primary_url: AnyHttpUrl = AnyHttpUrl(DEFAULT_PRIMARY_URL)
    secondary_url: AnyHttpUrl = AnyHttpUrl(DEFAULT_SECONDARY_URL)
    database_path: Path = Path(DEFAULT_DATABASE_PATH)
    token_budget: int = Field(default=50_000, gt=0, le=100_000_000)
    window_seconds: float = Field(default=60.0, gt=0, le=3600, allow_inf_nan=False)
    primary_timeout_seconds: float = Field(default=3.0, gt=0, le=120, allow_inf_nan=False)
    secondary_timeout_seconds: float = Field(default=3.0, gt=0, le=120, allow_inf_nan=False)
    database_timeout_seconds: float = Field(default=5.0, gt=0, le=60, allow_inf_nan=False)
    tenant_api_keys: tuple[SecretStr, ...] = Field(default=(), exclude=True)
    fingerprint_key: SecretStr = Field(default=SecretStr(""), exclude=True)

    @field_validator("primary_url", "secondary_url")
    @classmethod
    def plain_endpoint(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Provider URL must not contain credentials, query or fragment")
        return value

    @field_validator("database_path")
    @classmethod
    def usable_path(cls, value: Path) -> Path:
        if not str(value).strip() or str(value) == ":memory:":
            raise ValueError("Task 4 requires an on-disk SQLite path")
        return value

    @field_validator("tenant_api_keys")
    @classmethod
    def usable_keys(cls, value: tuple[SecretStr, ...]) -> tuple[SecretStr, ...]:
        seen: set[str] = set()
        for key in value:
            secret = key.get_secret_value()
            if not 1 <= len(secret) <= 4096 or secret in seen:
                raise ValueError("Tenant API keys must be nonempty and distinct")
            seen.add(secret)
        return value

    @property
    def pepper(self) -> bytes:
        return self.fingerprint_key.get_secret_value().encode("utf-8")


def _keys(raw: str) -> tuple[SecretStr, ...]:
    return tuple(SecretStr(part) for part in (item.strip() for item in raw.split(",")) if part)


def from_environment() -> Settings:
    try:
        return Settings(
            primary_url=os.environ.get("QUILR_PRIMARY_MODEL_URL", DEFAULT_PRIMARY_URL),
            secondary_url=os.environ.get("QUILR_SECONDARY_MODEL_URL", DEFAULT_SECONDARY_URL),
            database_path=os.environ.get("QUILR_RATE_LIMIT_DB_PATH", DEFAULT_DATABASE_PATH),
            token_budget=os.environ.get("QUILR_RATE_LIMIT_TOKENS", "50000"),
            window_seconds=os.environ.get("QUILR_RATE_LIMIT_WINDOW_SECONDS", "60"),
            primary_timeout_seconds=os.environ.get("QUILR_PRIMARY_TIMEOUT_SECONDS", "3"),
            secondary_timeout_seconds=os.environ.get("QUILR_SECONDARY_TIMEOUT_SECONDS", "3"),
            tenant_api_keys=_keys(os.environ.get("QUILR_TENANT_API_KEYS", "")),
            fingerprint_key=os.environ.get("QUILR_TENANT_FINGERPRINT_KEY", ""),
        )
    except ValidationError:
        raise ValueError("Invalid Task 4 configuration; check documented settings") from None
