"""Admit against the persisted quota, then run primary with one policy-based fallback."""

import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .errors import (
    QUOTA_STORAGE_UNAVAILABLE,
    RATE_LIMIT_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    GatewayError,
)
from .limiter import QuotaExceeded, QuotaStorageError, Reservation, TokenWindowLimiter
from .providers import FALLBACK_FAILURES, Completion, ProviderError, complete
from .schemas import CompletionRequest, CompletionResponse, Usage
from .tokens import count_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Attempt:
    name: str
    url: str
    timeout: float
    scenario: str
    delay_ms: int


class ModelRouter:
    def __init__(
        self, client: httpx.AsyncClient, settings: Settings, limiter: TokenWindowLimiter
    ) -> None:
        self._client = client
        self._settings = settings
        self._limiter = limiter

    def _attempts(self, request: CompletionRequest) -> tuple[Attempt, Attempt]:
        demo = request.demo
        return (
            Attempt(
                "primary",
                str(self._settings.primary_url),
                self._settings.primary_timeout_seconds,
                demo.primary_scenario if demo else "ok",
                demo.primary_delay_ms if demo else 0,
            ),
            Attempt(
                "secondary",
                str(self._settings.secondary_url),
                self._settings.secondary_timeout_seconds,
                demo.secondary_scenario if demo else "ok",
                demo.secondary_delay_ms if demo else 0,
            ),
        )

    async def _call(self, attempt: Attempt, request: CompletionRequest) -> Completion:
        return await complete(
            self._client,
            name=attempt.name,
            url=attempt.url,
            payload={
                "prompt": request.prompt,
                "max_output_tokens": request.max_output_tokens,
                "scenario": attempt.scenario,
                "delay_ms": attempt.delay_ms,
            },
            timeout=attempt.timeout,
        )

    async def _admit(self, tenant: str, reserved: int) -> Reservation:
        if reserved > self._limiter.token_budget:
            # A request larger than the whole window can never be served.
            logger.info("Request rejected: reservation exceeds the configured budget")
            raise GatewayError(RATE_LIMIT_EXCEEDED)
        try:
            return await self._limiter.reserve(tenant, reserved)
        except QuotaExceeded:
            logger.info("Request rejected before provider I/O: tenant quota exhausted")
            raise GatewayError(RATE_LIMIT_EXCEEDED) from None
        except QuotaStorageError:
            raise GatewayError(QUOTA_STORAGE_UNAVAILABLE) from None

    async def _charge(self, reservation: Reservation, completion: Completion) -> int:
        try:
            return await self._limiter.reconcile(reservation, completion.total_tokens)
        except QuotaStorageError:
            # The reservation already stands; keep serving the completed request.
            return reservation.tokens

    async def complete(self, tenant: str, request: CompletionRequest) -> CompletionResponse:
        """Reserve once per logical request; a fallback attempt is not charged twice."""
        reserved = count_tokens(request.prompt) + request.max_output_tokens
        reservation = await self._admit(tenant, reserved)
        primary, secondary = self._attempts(request)
        fallback_used = False
        try:
            completion = await self._call(primary, request)
        except ProviderError as exc:
            if exc.failure not in FALLBACK_FAILURES:
                logger.warning("Primary provider failed without a fallback trigger")
                raise GatewayError(UPSTREAM_UNAVAILABLE) from None
            logger.info("Primary provider %s; trying the secondary provider", exc.failure)
            try:
                completion = await self._call(secondary, request)
            except ProviderError:
                logger.warning("Secondary provider failed after fallback")
                raise GatewayError(UPSTREAM_UNAVAILABLE) from None
            fallback_used = True
        charged = await self._charge(reservation, completion)
        return CompletionResponse(
            provider=completion.provider,
            fallback_used=fallback_used,
            text=completion.text,
            usage=Usage(
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                total_tokens=completion.total_tokens,
                reserved_tokens=reservation.tokens,
                charged_tokens=charged,
            ),
        )
