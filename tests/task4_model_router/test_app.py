"""HTTP surface: tenant identity, request limits and standardized error payloads."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from quilr_assessment.mocks.completion import UPSTREAM_LEAK
from quilr_assessment.mocks.completion import create_app as create_mock
from quilr_assessment.task4_model_router.app import MAX_REQUEST_BYTES, create_app
from quilr_assessment.task4_model_router.config import Settings
from quilr_assessment.task4_model_router.tenants import fingerprint

pytestmark = pytest.mark.asyncio

KEY = "tenant-a-key"
OTHER_KEY = "tenant-b-key"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        **{
            "primary_url": "http://mock.invalid/primary/completions",
            "secondary_url": "http://mock.invalid/secondary/completions",
            "database_path": tmp_path / "quota.sqlite3",
            "primary_timeout_seconds": 0.5,
            "secondary_timeout_seconds": 0.5,
            **overrides,
        }
    )


@asynccontextmanager
async def gateway(
    tmp_path: Path, *, mock: FastAPI | None = None, **overrides
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI, FastAPI]]:
    provider = mock if mock is not None else create_mock()
    app = create_app(
        settings_for(tmp_path, **overrides), transport=httpx.ASGITransport(app=provider)
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            yield client, app, provider


async def test_successful_completion_returns_the_documented_envelope(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"provider", "fallback_used", "text", "usage"}
    assert body["provider"] == "primary"
    assert body["fallback_used"] is False
    assert set(body["usage"]) == {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reserved_tokens",
        "charged_tokens",
    }
    assert provider.state.requests == {"primary": 1, "secondary": 0}


async def test_primary_429_and_timeout_fall_back_over_http(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        rate_limited = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "demo": {"primary_scenario": "rate_limited"}},
        )
        timed_out = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "demo": {"primary_delay_ms": 2000}},
        )
    for response in (rate_limited, timed_out):
        assert response.status_code == 200
        assert response.json()["provider"] == "secondary"
        assert response.json()["fallback_used"] is True
    assert provider.state.requests == {"primary": 2, "secondary": 2}


async def test_upstream_error_bodies_never_reach_the_client(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={
                "prompt": "Explain retries",
                "demo": {"primary_scenario": "rate_limited", "secondary_scenario": "server_error"},
            },
        )
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "UPSTREAM_UNAVAILABLE",
            "message": "Unable to complete the request using the available model providers.",
        }
    }
    for fragment in ("Traceback", "/opt/models", "sk-mock-upstream-secret", "10.4.2.7", "429"):
        assert fragment not in response.text
    assert UPSTREAM_LEAK not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": f"Token {KEY}"},
        {"Authorization": "Bearer bad token"},
        {"Authorization": "Bearer " + "a" * 5000},
    ],
)
async def test_missing_or_malformed_credentials_are_rejected(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions",
            headers={"Content-Type": "application/json", **headers},
            json={"prompt": "Explain retries"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"
    assert provider.state.requests == {"primary": 0, "secondary": 0}


async def test_configured_allowlist_rejects_unknown_keys(tmp_path: Path) -> None:
    async with gateway(tmp_path, tenant_api_keys=(KEY,)) as (client, app, provider):
        allowed = await client.post(
            "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
        )
        refused = await client.post(
            "/v1/completions",
            headers={**HEADERS, "Authorization": f"Bearer {OTHER_KEY}"},
            json={"prompt": "Explain retries"},
        )
    assert allowed.status_code == 200
    assert refused.status_code == 401
    assert provider.state.requests["primary"] == 1


async def test_tenants_hold_independent_quotas_over_http(tmp_path: Path) -> None:
    async with gateway(tmp_path, token_budget=300) as (client, app, provider):
        first = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "max_output_tokens": 290},
        )
        second = await client.post(
            "/v1/completions",
            headers=HEADERS,
            json={"prompt": "Explain retries", "max_output_tokens": 290},
        )
        other = await client.post(
            "/v1/completions",
            headers={**HEADERS, "Authorization": f"Bearer {OTHER_KEY}"},
            json={"prompt": "Explain retries", "max_output_tokens": 290},
        )
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert other.status_code == 200
    # The rejected request never reached a provider.
    assert provider.state.requests["primary"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": 5},
        {"prompt": None},
        {"prompt": "hi", "unexpected": 1},
        {"prompt": "hi", "max_output_tokens": 0},
        {"prompt": "hi", "max_output_tokens": 4097},
        {"prompt": "hi", "max_output_tokens": "64"},
        {"prompt": "hi", "max_output_tokens": 1.5},
        {"prompt": "hi", "demo": {"primary_scenario": "unknown"}},
        {"prompt": "hi", "demo": {"primary_delay_ms": -1}},
        {"prompt": "hi", "demo": []},
        {"prompt": "a" * 8001},
        [],
        "text",
    ],
)
async def test_invalid_requests_are_rejected_before_any_provider_call(
    tmp_path: Path, payload: object
) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post("/v1/completions", headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "INVALID_REQUEST", "message": "Invalid completion request."}
    }
    assert provider.state.requests == {"primary": 0, "secondary": 0}


async def test_malformed_json_body_is_rejected(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post("/v1/completions", headers=HEADERS, content=b"{oops")
    assert response.status_code == 422
    assert provider.state.requests["primary"] == 0


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "", "application/json-patch+json"],
)
async def test_unsupported_media_types_are_rejected(tmp_path: Path, content_type: str) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": content_type},
            content=b'{"prompt": "hi"}',
        )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_compressed_request_bodies_are_rejected(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions",
            headers={**HEADERS, "Content-Encoding": "gzip"},
            content=b'{"prompt": "hi"}',
        )
    assert response.status_code == 415


async def test_oversized_bodies_are_rejected_before_parsing(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        response = await client.post(
            "/v1/completions",
            headers=HEADERS,
            content=b'{"prompt": "' + b"a" * (MAX_REQUEST_BYTES + 64) + b'"}',
        )
    assert response.status_code == 413
    assert provider.state.requests["primary"] == 0


async def test_quota_storage_failure_returns_a_safe_unavailable_error(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        app.state.limiter.database_path.unlink()
        app.state.limiter.database_path.mkdir()
        response = await client.post(
            "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "QUOTA_STORAGE_UNAVAILABLE"
    assert provider.state.requests["primary"] == 0


async def test_quota_state_survives_restarting_the_application(tmp_path: Path) -> None:
    payload = {"prompt": "Explain retries", "max_output_tokens": 290}
    async with gateway(tmp_path, token_budget=300) as (client, app, provider):
        assert (
            await client.post("/v1/completions", headers=HEADERS, json=payload)
        ).status_code == 200
    async with gateway(tmp_path, token_budget=300) as (client, app, provider):
        response = await client.post("/v1/completions", headers=HEADERS, json=payload)
    assert response.status_code == 429
    assert provider.state.requests["primary"] == 0


async def test_no_caller_headers_or_credentials_cross_the_provider_boundary(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Headers] = []
    mock = create_mock()

    async def spy(request):
        seen.append(request.headers)
        return await httpx.ASGITransport(app=mock).handle_async_request(request)

    app = create_app(settings_for(tmp_path), transport=httpx.MockTransport(spy))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            await client.post(
                "/v1/completions",
                headers={**HEADERS, "Cookie": "session=abc", "X-Trace": "caller"},
                json={"prompt": "Explain retries"},
            )
    assert seen
    for headers in seen:
        assert "authorization" not in headers
        assert "cookie" not in headers
        assert "x-trace" not in headers
        assert KEY not in str(headers)


async def test_logs_never_contain_the_api_key_prompt_or_upstream_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    prompt = "Refund card 4111 1111 1111 1111 for jordan.lee@example.com"
    with caplog.at_level(logging.DEBUG):
        async with gateway(tmp_path, token_budget=200) as (client, app, provider):
            await client.post(
                "/v1/completions",
                headers=HEADERS,
                json={
                    "prompt": prompt,
                    "demo": {"primary_scenario": "rate_limited"},
                },
            )
            await client.post(
                "/v1/completions",
                headers=HEADERS,
                json={"prompt": prompt, "max_output_tokens": 4000},
            )
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("quilr_assessment")
    )
    assert ours
    for secret in (KEY, prompt, "jordan.lee@example.com", "4111", UPSTREAM_LEAK):
        assert secret not in ours
    assert fingerprint(KEY) not in ours
    assert "Traceback" not in ours


async def test_the_database_file_holds_fingerprints_and_no_raw_keys(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        await client.post("/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"})
        path = app.state.limiter.database_path
    stored = path.read_bytes()
    assert KEY.encode() not in stored
    assert b"Explain retries" not in stored
    assert fingerprint(KEY).encode() in stored


async def test_unknown_routes_and_methods_do_not_expose_a_schema(tmp_path: Path) -> None:
    async with gateway(tmp_path) as (client, app, provider):
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/v1/completions")).status_code == 405


async def test_unexpected_failures_return_the_standardized_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A handler defect must still produce the envelope, never a traceback body."""
    app = create_app(settings_for(tmp_path), transport=httpx.ASGITransport(app=create_mock()))

    async def broken(tenant: str, request: object) -> None:
        raise RuntimeError(f"internal defect at /srv/gateway/router.py with {KEY}")

    async with app.router.lifespan_context(app):
        app.state.router.complete = broken
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            with caplog.at_level(logging.DEBUG):
                response = await client.post(
                    "/v1/completions", headers=HEADERS, json={"prompt": "Explain retries"}
                )
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "The gateway could not process this request.",
        }
    }
    assert KEY not in response.text
    assert "/srv/gateway" not in response.text
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("quilr_assessment")
    )
    assert "Unhandled gateway failure" in ours
    assert KEY not in ours
    assert "Traceback" not in ours
