"""Strict client contract; local demo controls stay in an explicit optional block."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PROMPT_CHARS = 8000
MAX_OUTPUT_TOKENS = 4096

Scenario = Literal[
    "ok", "rate_limited", "slow", "server_error", "bad_request", "unauthorized", "malformed"
]


class DemoControls(BaseModel):
    """Steers the local mock providers only; a real adapter would ignore this."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    primary_scenario: Scenario = "ok"
    secondary_scenario: Scenario = "ok"
    primary_delay_ms: int = Field(default=0, ge=0, le=10_000)
    secondary_delay_ms: int = Field(default=0, ge=0, le=10_000)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    max_output_tokens: int = Field(default=256, ge=1, le=MAX_OUTPUT_TOKENS)
    demo: DemoControls | None = None

    @field_validator("prompt")
    @classmethod
    def valid_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank")
        value.encode("utf-8")
        return value


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    charged_tokens: int = Field(ge=0)


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["primary", "secondary"]
    fallback_used: bool
    text: str
    usage: Usage
