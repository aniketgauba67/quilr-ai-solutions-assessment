"""Local generation inputs and provider settings; no credentials are required."""

import os
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    prompt: str = Field(min_length=1, max_length=4000)
    scenario: Literal[
        "ordinary", "email", "ssn", "card", "mixed", "slow", "http_error", "malformed", "disconnect"
    ] = "mixed"
    delay_ms: int = Field(default=0, ge=0, le=1000)
    chunk_size: int = Field(default=7, ge=1, le=256)

    @field_validator("prompt")
    @classmethod
    def valid_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank")
        value.encode("utf-8")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:9001/stream")
    timeout_seconds: float = Field(default=10.0, gt=0, le=120, allow_inf_nan=False)

    @field_validator("provider_url")
    @classmethod
    def plain_endpoint(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Provider URL must not contain credentials, query or fragment")
        return value


def from_environment() -> Settings:
    try:
        return Settings(
            provider_url=os.environ.get("QUILR_LLM_STREAM_URL", "http://127.0.0.1:9001/stream"),
            timeout_seconds=os.environ.get("QUILR_LLM_TIMEOUT_SECONDS", "10"),
        )
    except ValidationError:
        raise ValueError("Invalid Task 3 configuration; check documented settings") from None
