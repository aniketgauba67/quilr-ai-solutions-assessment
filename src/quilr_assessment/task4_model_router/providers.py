"""One bounded provider call shape; upstream bodies never leave this module."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 256 * 1024
MAX_TEXT_CHARS = 100_000
Failure = Literal["rate_limited", "timeout", "http_error", "invalid_response", "transport"]
FALLBACK_FAILURES: frozenset[Failure] = frozenset({"rate_limited", "timeout"})


class ProviderError(Exception):
    """Classifies a failure without retaining any upstream body, URL or exception."""

    def __init__(self, name: str, failure: Failure) -> None:
        self.name = name
        self.failure = failure
        super().__init__(f"{name} provider {failure}")


class _Usage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    prompt_tokens: int = Field(ge=0, le=10_000_000)
    completion_tokens: int = Field(ge=0, le=10_000_000)


class _ProviderPayload(BaseModel):
    # Unknown provider metadata is discarded rather than forwarded to the client.
    model_config = ConfigDict(strict=True, extra="ignore")

    text: str = Field(max_length=MAX_TEXT_CHARS)
    usage: _Usage


@dataclass(frozen=True, slots=True)
class Completion:
    provider: str
    text: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def complete(
    client: httpx.AsyncClient,
    *,
    name: str,
    url: str,
    payload: dict[str, Any],
    timeout: float,
) -> Completion:
    """Run one attempt under a total deadline covering connect, send and body read."""
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code == 429:
                    raise ProviderError(name, "rate_limited")
                if response.status_code != 200:
                    # The status is a safe internal diagnostic; the body is not read.
                    logger.warning(
                        "Model provider %s returned status %d", name, response.status_code
                    )
                    raise ProviderError(name, "http_error")
                if (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ProviderError(name, "invalid_response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderError(name, "invalid_response")
                    body.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("Model provider attempt exceeded its deadline")
        raise ProviderError(name, "timeout") from None
    except httpx.HTTPError:
        logger.warning("Model provider transport failure")
        raise ProviderError(name, "transport") from None
    try:
        parsed = _ProviderPayload.model_validate_json(bytes(body))
    except (ValidationError, ValueError):
        logger.warning("Model provider returned an unsupported payload")
        raise ProviderError(name, "invalid_response") from None
    return Completion(
        provider=name,
        text=parsed.text,
        prompt_tokens=parsed.usage.prompt_tokens,
        completion_tokens=parsed.usage.completion_tokens,
    )
