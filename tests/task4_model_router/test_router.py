"""Admission ordering, the deliberate fallback policy and reservation accounting."""

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from quilr_assessment.task4_model_router.config import Settings
from quilr_assessment.task4_model_router.errors import (
    QUOTA_STORAGE_UNAVAILABLE,
    RATE_LIMIT_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    GatewayError,
)
from quilr_assessment.task4_model_router.limiter import (
    QuotaStorageError,
    Reservation,
    TokenWindowLimiter,
)
from quilr_assessment.task4_model_router.router import ModelRouter
from quilr_assessment.task4_model_router.schemas import CompletionRequest, DemoControls
from quilr_assessment.task4_model_router.tenants import fingerprint
from quilr_assessment.task4_model_router.tokens import count_tokens

pytestmark = pytest.mark.asyncio

TENANT = fingerprint("tenant-a-key")
SENTINEL = "sk-upstream-secret /opt/models/handler.py"
PRIMARY_URL = "http://primary.invalid/completions"
SECONDARY_URL = "http://secondary.invalid/completions"


class Providers:
    """Records every attempt and replays a scripted response per provider."""

    def __init__(self, primary, secondary=None) -> None:
        self.calls: list[str] = []
        self._responses = {"primary": primary, "secondary": secondary}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        name = "primary" if "primary" in str(request.url) else "secondary"
        self.calls.append(name)
        response = self._responses[name]
        if response is None:
            raise AssertionError(f"{name} provider must not be contacted")
        return await response(request) if callable(response) else response


def ok(name: str, *, prompt_tokens: int = 4, completion_tokens: int = 20) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "text": f"{name} reply",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def failing(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": SENTINEL})


def stalled(seconds: float = 10.0):
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        return ok("slow")

    return respond


def build(
    tmp_path: Path, providers: Providers, *, budget: int = 50_000, timeout: float = 0.1, clock=None
) -> tuple[ModelRouter, TokenWindowLimiter, httpx.AsyncClient]:
    settings = Settings(
        primary_url=PRIMARY_URL,
        secondary_url=SECONDARY_URL,
        database_path=tmp_path / "quota.sqlite3",
        token_budget=budget,
        primary_timeout_seconds=timeout,
        secondary_timeout_seconds=timeout,
    )
    limiter = TokenWindowLimiter(
        settings.database_path,
        token_budget=budget,
        window_seconds=settings.window_seconds,
        clock=clock,
    )
    limiter.initialize_sync()
    client = httpx.AsyncClient(transport=httpx.MockTransport(providers))
    return ModelRouter(client, settings, limiter), limiter, client


def request(prompt: str = "Summarize the incident", **kwargs) -> CompletionRequest:
    demo = kwargs.pop("demo", None)
    return CompletionRequest(prompt=prompt, demo=DemoControls(**demo) if demo else None, **kwargs)


async def test_primary_success_never_contacts_the_secondary(tmp_path: Path) -> None:
    providers = Providers(ok("primary"))
    router, limiter, client = build(tmp_path, providers)
    async with client:
        result = await router.complete(TENANT, request())
    assert providers.calls == ["primary"]
    assert result.provider == "primary"
    assert result.fallback_used is False
    assert result.text == "primary reply"


@pytest.mark.parametrize("status", [429])
async def test_primary_rate_limiting_falls_back_once(tmp_path: Path, status: int) -> None:
    providers = Providers(failing(status), ok("secondary"))
    router, limiter, client = build(tmp_path, providers)
    async with client:
        result = await router.complete(TENANT, request())
    assert providers.calls == ["primary", "secondary"]
    assert result.provider == "secondary"
    assert result.fallback_used is True


async def test_primary_timeout_falls_back_once(tmp_path: Path) -> None:
    providers = Providers(stalled(), ok("secondary"))
    router, limiter, client = build(tmp_path, providers, timeout=0.05)
    async with client:
        result = await router.complete(TENANT, request())
    assert providers.calls == ["primary", "secondary"]
    assert result.fallback_used is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500, 502, 503])
async def test_other_primary_failures_do_not_fall_back(tmp_path: Path, status: int) -> None:
    providers = Providers(failing(status), None)
    router, limiter, client = build(tmp_path, providers)
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request())
    assert providers.calls == ["primary"]
    assert raised.value.code == UPSTREAM_UNAVAILABLE


async def test_invalid_primary_payload_does_not_fall_back(tmp_path: Path) -> None:
    providers = Providers(
        httpx.Response(200, content=b"{oops", headers={"Content-Type": "application/json"}), None
    )
    router, limiter, client = build(tmp_path, providers)
    async with client:
        with pytest.raises(GatewayError):
            await router.complete(TENANT, request())
    assert providers.calls == ["primary"]


async def test_transport_failure_does_not_fall_back(tmp_path: Path) -> None:
    async def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(SENTINEL)

    providers = Providers(broken, None)
    router, limiter, client = build(tmp_path, providers)
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request())
    assert raised.value.code == UPSTREAM_UNAVAILABLE
    assert SENTINEL not in raised.value.message


async def test_secondary_failure_returns_one_standardized_error(tmp_path: Path) -> None:
    providers = Providers(failing(429), failing(500))
    router, limiter, client = build(tmp_path, providers)
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request())
    assert providers.calls == ["primary", "secondary"]
    assert raised.value.payload() == {
        "error": {
            "code": UPSTREAM_UNAVAILABLE,
            "message": "Unable to complete the request using the available model providers.",
        }
    }


async def test_secondary_timeout_after_primary_timeout_is_still_bounded(tmp_path: Path) -> None:
    providers = Providers(stalled(), stalled())
    router, limiter, client = build(tmp_path, providers, timeout=0.05)
    async with client:
        async with asyncio.timeout(5):
            with pytest.raises(GatewayError):
                await router.complete(TENANT, request())
    assert providers.calls == ["primary", "secondary"]


async def test_quota_rejection_contacts_no_provider(tmp_path: Path) -> None:
    providers = Providers(None, None)
    router, limiter, client = build(tmp_path, providers, budget=100)
    limiter.reserve_sync(TENANT, 100)
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request())
    assert providers.calls == []
    assert raised.value.code == RATE_LIMIT_EXCEEDED
    assert raised.value.status == 429


async def test_request_larger_than_the_whole_budget_is_rejected_without_a_row(
    tmp_path: Path,
) -> None:
    providers = Providers(None, None)
    router, limiter, client = build(tmp_path, providers, budget=100)
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request(max_output_tokens=4096))
    assert raised.value.code == RATE_LIMIT_EXCEEDED
    assert limiter.usage_sync(TENANT) == 0


async def test_storage_failure_fails_closed_without_provider_calls(tmp_path: Path) -> None:
    providers = Providers(None, None)
    router, limiter, client = build(tmp_path, providers)
    limiter.database_path.unlink()
    limiter.database_path.mkdir()
    async with client:
        with pytest.raises(GatewayError) as raised:
            await router.complete(TENANT, request())
    assert providers.calls == []
    assert raised.value.code == QUOTA_STORAGE_UNAVAILABLE
    assert raised.value.status == 503


async def test_reservation_covers_prompt_plus_requested_output(tmp_path: Path) -> None:
    providers = Providers(ok("primary", prompt_tokens=4, completion_tokens=6))
    router, limiter, client = build(tmp_path, providers)
    prompt = "Summarize the incident"
    async with client:
        result = await router.complete(TENANT, request(prompt, max_output_tokens=64))
    assert result.usage.reserved_tokens == count_tokens(prompt) + 64
    assert result.usage.total_tokens == 10
    assert result.usage.charged_tokens == 10
    assert limiter.usage_sync(TENANT) == 10


async def test_a_failed_request_keeps_its_reservation_until_expiry(tmp_path: Path) -> None:
    providers = Providers(failing(500), None)
    router, limiter, client = build(tmp_path, providers)
    async with client:
        with pytest.raises(GatewayError):
            await router.complete(TENANT, request(max_output_tokens=100))
    # Usage after a timeout or failure is unknown, so the conservative reserve stands.
    assert limiter.usage_sync(TENANT) == count_tokens("Summarize the incident") + 100


async def test_a_fallback_request_is_charged_once(tmp_path: Path) -> None:
    providers = Providers(failing(429), ok("secondary", prompt_tokens=4, completion_tokens=6))
    router, limiter, client = build(tmp_path, providers)
    async with client:
        result = await router.complete(TENANT, request())
    assert result.usage.charged_tokens == 10
    assert limiter.usage_sync(TENANT) == 10


async def test_reported_usage_cannot_exceed_the_authorized_reservation(tmp_path: Path) -> None:
    providers = Providers(ok("primary", prompt_tokens=10**6, completion_tokens=10**6))
    router, limiter, client = build(tmp_path, providers)
    async with client:
        result = await router.complete(TENANT, request(max_output_tokens=16))
    assert result.usage.charged_tokens == result.usage.reserved_tokens
    assert limiter.usage_sync(TENANT) == result.usage.reserved_tokens


async def test_reconciliation_failure_does_not_fail_a_completed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = Providers(ok("primary"))
    router, limiter, client = build(tmp_path, providers)

    def broken(reservation: Reservation, actual: int) -> int:
        raise QuotaStorageError()

    monkeypatch.setattr(limiter, "reconcile_sync", broken)
    async with client:
        result = await router.complete(TENANT, request(max_output_tokens=32))
    assert result.usage.charged_tokens == result.usage.reserved_tokens


async def test_repeated_requests_consume_the_window_until_rejection(tmp_path: Path) -> None:
    providers = Providers(ok("primary", prompt_tokens=1, completion_tokens=1))
    router, limiter, client = build(tmp_path, providers, budget=200)
    async with client:
        for _ in range(20):
            await router.complete(TENANT, request(max_output_tokens=1))
        assert limiter.usage_sync(TENANT) == 40
        with pytest.raises(GatewayError):
            await router.complete(TENANT, request(max_output_tokens=161))


async def test_tenants_are_limited_independently(tmp_path: Path) -> None:
    providers = Providers(ok("primary", prompt_tokens=1, completion_tokens=1))
    other = fingerprint("tenant-b-key")
    router, limiter, client = build(tmp_path, providers, budget=100)
    limiter.reserve_sync(TENANT, 100)
    async with client:
        with pytest.raises(GatewayError):
            await router.complete(TENANT, request(max_output_tokens=1))
        result = await router.complete(other, request(max_output_tokens=1))
    assert result.provider == "primary"


async def test_caller_cancellation_propagates_without_a_fallback(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def stall(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(30)
        return ok("primary")

    providers = Providers(stall, None)
    router, limiter, client = build(tmp_path, providers, timeout=30)
    async with client:
        task = asyncio.create_task(router.complete(TENANT, request()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Cancellation is not a provider failure, so no fallback attempt is made.
    assert providers.calls == ["primary"]


async def test_demo_controls_reach_only_their_own_provider(tmp_path: Path) -> None:
    seen: dict[str, dict] = {}

    async def record(request: httpx.Request) -> httpx.Response:
        import json

        name = "primary" if "primary" in str(request.url) else "secondary"
        seen[name] = json.loads(request.content)
        return failing(429) if name == "primary" else ok("secondary")

    providers = Providers(record, record)
    router, limiter, client = build(tmp_path, providers)
    async with client:
        await router.complete(
            TENANT,
            request(demo={"primary_scenario": "rate_limited", "secondary_delay_ms": 5}),
        )
    assert seen["primary"]["scenario"] == "rate_limited"
    assert seen["primary"]["delay_ms"] == 0
    assert seen["secondary"]["scenario"] == "ok"
    assert seen["secondary"]["delay_ms"] == 5


async def test_router_logs_stay_fixed_and_never_include_the_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prompt = "Contact jordan.lee@example.com about card 4111 1111 1111 1111"
    providers = Providers(failing(429), failing(500))
    router, limiter, client = build(tmp_path, providers)
    with caplog.at_level(logging.DEBUG):
        async with client:
            with pytest.raises(GatewayError):
                await router.complete(TENANT, request(prompt))
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("quilr_assessment")
    )
    assert ours
    assert prompt not in ours
    assert "jordan.lee@example.com" not in ours
    assert SENTINEL not in ours
    assert PRIMARY_URL not in ours
    assert TENANT not in ours
