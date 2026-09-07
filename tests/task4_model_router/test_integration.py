"""Real loopback HTTP: fallback, deadline cleanup, persistence and the 3000 ms default."""

import asyncio
import socket
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from quilr_assessment.mocks.completion import create_app as create_mock
from quilr_assessment.task4_model_router.app import create_app
from quilr_assessment.task4_model_router.config import Settings, from_environment
from quilr_assessment.task4_model_router.tenants import fingerprint

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

KEY = "tenant-a-key"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


@asynccontextmanager
async def serve(app: FastAPI | str) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                factory=isinstance(app, str),
                ws="none",
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=1,
            )
        )
        # These servers share pytest's process; signal ownership stays with pytest.
        with patch.object(server, "capture_signals", nullcontext):
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            pytest.fail("Uvicorn exited before startup")
                        await asyncio.sleep(0.01)
                yield f"http://127.0.0.1:{port}"
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)


@asynccontextmanager
async def stack(tmp_path: Path, **overrides) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    mock = create_mock()
    async with serve(mock) as provider_url:
        settings = Settings(
            primary_url=f"{provider_url}/primary/completions",
            secondary_url=f"{provider_url}/secondary/completions",
            database_path=tmp_path / "quota.sqlite3",
            **overrides,
        )
        app = create_app(settings)
        async with serve(app) as gateway_url:
            async with httpx.AsyncClient(base_url=gateway_url, timeout=20) as client:
                yield client, mock


async def test_end_to_end_success_over_real_sockets(tmp_path: Path) -> None:
    async with stack(tmp_path) as (client, mock):
        response = await client.post(
            "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "primary"
    assert mock.state.requests == {"primary": 1, "secondary": 0}
    assert (tmp_path / "quota.sqlite3").is_file()


async def test_primary_429_fails_over_to_the_secondary(tmp_path: Path) -> None:
    async with stack(tmp_path) as (client, mock):
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "demo": {"primary_scenario": "rate_limited"}},
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "secondary"
    assert response.json()["fallback_used"] is True
    assert mock.state.requests == {"primary": 1, "secondary": 1}
    assert mock.state.completed["secondary"] == 1


async def test_primary_deadline_fails_over_without_waiting_for_the_primary(
    tmp_path: Path,
) -> None:
    """The gateway abandons its own attempt; it cannot stop a remote server's work.

    Uvicorn does not cancel a non-streaming handler when its client disconnects, so
    the assertions here cover what the gateway controls: it stops waiting at the
    deadline, answers from the secondary, and never uses the late primary response.
    """
    async with stack(tmp_path, primary_timeout_seconds=0.3, secondary_timeout_seconds=3.0) as (
        client,
        mock,
    ):
        started = time.monotonic()
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "demo": {"primary_delay_ms": 5000}},
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert response.json()["provider"] == "secondary"
        assert response.json()["fallback_used"] is True
        # The primary is still sleeping upstream; its result was not awaited.
        assert mock.state.completed["primary"] == 0
        assert elapsed < 4
        # The abandoned attempt did not damage the connection pool.
        healthy = await client.post(
            "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
        )
        assert healthy.status_code == 200
        assert healthy.json()["provider"] == "primary"


async def test_both_providers_failing_returns_the_standardized_payload(tmp_path: Path) -> None:
    async with stack(tmp_path, primary_timeout_seconds=0.3, secondary_timeout_seconds=0.3) as (
        client,
        mock,
    ):
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={
                "prompt": "Explain retries",
                "demo": {"primary_scenario": "rate_limited", "secondary_delay_ms": 5000},
            },
        )
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "UPSTREAM_UNAVAILABLE",
            "message": "Unable to complete the request using the available model providers.",
        }
    }
    for fragment in ("Traceback", "/opt/models", "sk-mock-upstream-secret", "10.4.2.7"):
        assert fragment not in response.text
    assert mock.state.completed["secondary"] == 0


async def test_quota_is_enforced_and_persists_across_gateway_restarts(tmp_path: Path) -> None:
    payload = {"prompt": "Explain retries", "max_output_tokens": 290}
    async with stack(tmp_path, token_budget=300) as (client, mock):
        assert (
            await client.post("/v1/completions", headers=HEADERS, json=payload)
        ).status_code == 200
        rejected = await client.post("/v1/completions", headers=HEADERS, json=payload)
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert mock.state.requests["primary"] == 1

    async with stack(tmp_path, token_budget=300) as (client, mock):
        after_restart = await client.post("/v1/completions", headers=HEADERS, json=payload)
    assert after_restart.status_code == 429
    assert mock.state.requests == {"primary": 0, "secondary": 0}


async def test_concurrent_http_requests_cannot_exceed_the_budget(tmp_path: Path) -> None:
    """A slow provider keeps every reservation open, so admission alone decides."""
    payload = {
        "prompt": "Explain retries",
        "max_output_tokens": 90,
        "demo": {"primary_delay_ms": 300},
    }
    async with stack(tmp_path, token_budget=500) as (client, mock):
        responses = await asyncio.gather(
            *(client.post("/v1/completions", headers=HEADERS, json=payload) for _ in range(12))
        )
        statuses = [response.status_code for response in responses]
        connection = sqlite3.connect(tmp_path / "quota.sqlite3")
        try:
            (reserved,) = connection.execute("SELECT SUM(tokens) FROM reservations").fetchone()
        finally:
            connection.close()
    assert statuses.count(200) == 5
    assert statuses.count(429) == 7
    assert mock.state.requests["primary"] == 5
    assert reserved <= 500


async def test_stored_rows_never_contain_the_raw_api_key(tmp_path: Path) -> None:
    async with stack(tmp_path) as (client, mock):
        await client.post("/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"})
    connection = sqlite3.connect(tmp_path / "quota.sqlite3")
    try:
        rows = connection.execute("SELECT tenant, tokens, created_at FROM reservations").fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == [fingerprint(KEY)]
    assert KEY.encode() not in (tmp_path / "quota.sqlite3").read_bytes()


async def test_the_default_primary_deadline_is_exactly_3000_milliseconds() -> None:
    assert Settings().primary_timeout_seconds == 3.0
    assert Settings().secondary_timeout_seconds == 3.0


async def test_documented_environment_defaults_match_the_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "QUILR_PRIMARY_MODEL_URL",
        "QUILR_SECONDARY_MODEL_URL",
        "QUILR_RATE_LIMIT_DB_PATH",
        "QUILR_RATE_LIMIT_TOKENS",
        "QUILR_RATE_LIMIT_WINDOW_SECONDS",
        "QUILR_PRIMARY_TIMEOUT_SECONDS",
        "QUILR_SECONDARY_TIMEOUT_SECONDS",
        "QUILR_TENANT_API_KEYS",
        "QUILR_TENANT_FINGERPRINT_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = from_environment()
    assert settings.token_budget == 50_000
    assert settings.window_seconds == 60.0
    assert settings.primary_timeout_seconds == 3.0
    assert settings.tenant_api_keys == ()
    assert str(settings.database_path) == "var/rate_limit.sqlite3"


async def test_invalid_environment_configuration_is_reported_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUILR_PRIMARY_MODEL_URL", "http://user:secret@primary.invalid/x")
    with pytest.raises(ValueError) as raised:
        from_environment()
    assert "secret" not in str(raised.value)
    assert str(raised.value) == "Invalid Task 4 configuration; check documented settings"


async def test_uvicorn_factory_startup_serves_the_documented_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = create_mock()
    async with serve(mock) as provider_url:
        monkeypatch.setenv("QUILR_PRIMARY_MODEL_URL", f"{provider_url}/primary/completions")
        monkeypatch.setenv("QUILR_SECONDARY_MODEL_URL", f"{provider_url}/secondary/completions")
        monkeypatch.setenv("QUILR_RATE_LIMIT_DB_PATH", str(tmp_path / "quota.sqlite3"))
        async with serve("quilr_assessment.task4_model_router.app:create_app") as gateway_url:
            async with httpx.AsyncClient(base_url=gateway_url, timeout=20) as client:
                response = await client.post(
                    "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
                )
    assert response.status_code == 200
    assert response.json()["provider"] == "primary"


async def test_a_longer_secondary_deadline_is_not_preempted_by_the_client(
    tmp_path: Path,
) -> None:
    """The shared client timeout must cover the longer of the two attempt deadlines."""
    async with stack(tmp_path, primary_timeout_seconds=0.2, secondary_timeout_seconds=3.0) as (
        client,
        mock,
    ):
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={
                "prompt": "Explain retries",
                "demo": {"primary_scenario": "rate_limited", "secondary_delay_ms": 900},
            },
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "secondary"
    assert mock.state.completed["secondary"] == 1
