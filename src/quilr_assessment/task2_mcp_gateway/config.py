"""Only Task 2 settings; credentials are required and never shown in repr/errors."""

import os
import re
from hmac import compare_digest
from typing import Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .auth import TOKEN_PATTERN


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    admin_token: SecretStr = Field(exclude=True)
    viewer_token: SecretStr = Field(exclude=True)
    downstream_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:9000/mcp")
    timeout_seconds: float = Field(default=5.0, gt=0, le=30, allow_inf_nan=False)

    @field_validator("admin_token", "viewer_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not 1 <= len(token) <= 4096 or re.fullmatch(TOKEN_PATTERN, token) is None:
            raise ValueError("Token must use the Bearer credential alphabet")
        return value

    @field_validator("downstream_url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Downstream URL must not contain credentials, query or fragment")
        return value

    @model_validator(mode="after")
    def distinct_tokens(self) -> Self:
        if compare_digest(
            self.admin_token.get_secret_value(), self.viewer_token.get_secret_value()
        ):
            raise ValueError("Demo role tokens must be distinct")
        return self


def from_environment() -> Settings:
    try:
        return Settings(
            admin_token=os.environ.get("QUILR_ADMIN_TOKEN", ""),
            viewer_token=os.environ.get("QUILR_VIEWER_TOKEN", ""),
            downstream_url=os.environ.get("QUILR_MCP_DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp"),
            timeout_seconds=os.environ.get("QUILR_MCP_TIMEOUT_SECONDS", "5"),
        )
    except ValidationError:
        raise ValueError(
            "Invalid Task 2 configuration; check the documented environment settings"
        ) from None
