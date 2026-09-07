import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from quilr_assessment.task2_mcp_gateway import app as gateway
from quilr_assessment.task2_mcp_gateway.config import Settings
from quilr_assessment.task2_mcp_gateway.proxy import MAX_RESPONSE_BYTES

ADMIN = "test-admin-credential"
VIEWER = "test-viewer-credential"
SENTINEL = "secret-sentinel /private/internal.py https://internal.invalid"


def rpc(
    method: str = "tools/list", *, name: str | None = None, request_id: object = "req-1"
) -> dict:
    result = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if name is not None:
        result["params"] = {"name": name, "arguments": {}}
    return result


@asynccontextmanager
async def client_for(
    handler: Callable, *, timeout: float = 5.0
) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    settings = Settings(admin_token=ADMIN, viewer_token=VIEWER, timeout_seconds=timeout)
    app = gateway.create_app(settings, transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            yield client, app


def send_headers(token: str = VIEWER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [ADMIN, VIEWER])
@pytest.mark.parametrize(
    "method,name", [("tools/list", None), ("tools/call", "get_status"), ("ping", None)]
)
async def test_allowed_requests_preserve_bytes_and_use_only_safe_headers(
    token: str, method: str, name: str | None
) -> None:
    seen = []
    expected = (
        b'{ "jsonrpc": "2.0", "id": "req-1", "result": {"tools":[{"name":"admin_reset_key"}]} }\n'
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=expected,
            headers={
                "Content-Type": "application/json",
                "Set-Cookie": "secret=value",
                "X-Internal": SENTINEL,
            },
        )

    body = json.dumps(rpc(method, name=name), indent=2).encode()
    async with client_for(upstream) as (client, _):
        response = await client.post(
            "/mcp",
            content=body,
            headers={
                **send_headers(token),
                "Cookie": "caller=secret",
                "X-Api-Key": "private-key",
                "Connection": "X-Custom",
                "X-Custom": "private",
            },
        )
    assert response.status_code == 200
    assert response.content == expected
    assert len(seen) == 1 and seen[0].content == body
    assert str(seen[0].url) == "http://127.0.0.1:9000/mcp"
    for name in ("authorization", "cookie", "x-api-key", "x-custom"):
        assert name not in seen[0].headers
    assert seen[0].headers["accept"] == "application/json"
    assert "set-cookie" not in response.headers and "x-internal" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [0, "original-id", None, 2.5])
@pytest.mark.parametrize("tool", ["admin_reset_key", "admin_rotate_secret", "admin_"])
async def test_forbidden_call_has_exact_error_and_zero_downstream_requests(
    request_id: object, tool: str
) -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with client_for(upstream) as (client, _):
        response = await client.post(
            "/mcp", json=rpc("tools/call", name=tool, request_id=request_id), headers=send_headers()
        )
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert seen == []


@pytest.mark.asyncio
async def test_admin_protected_call_reaches_downstream() -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "req-1", "result": {"status": "simulated"}}
        )

    async with client_for(upstream) as (client, _):
        response = await client.post(
            "/mcp", json=rpc("tools/call", name="admin_reset_key"), headers=send_headers(ADMIN)
        )
    assert response.status_code == 200
    assert response.json()["result"] == {"status": "simulated"}
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer unknown"},
        [("Authorization", f"Bearer {VIEWER}"), ("Authorization", f"Bearer {ADMIN}")],
    ],
)
async def test_authentication_rejection_precedes_parsing_and_downstream(headers: object) -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with client_for(upstream) as (client, _):
        response = await client.post("/mcp", content=b"malformed", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "unauthenticated", "message": "Authentication required"}
    }
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,code",
    [
        (b"{", -32700),
        (b"[]", -32600),
        (b'{"jsonrpc":"2.0","id":1}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}', -32602),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}', -32602),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":true}}', -32602),
        (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_status","name":"admin_reset_key"}}',
            -32700,
        ),
    ],
)
async def test_malformed_rpc_never_reaches_downstream(body: bytes, code: int) -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with client_for(upstream) as (client, _):
        response = await client.post("/mcp", content=body, headers=send_headers())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert not seen


@pytest.mark.asyncio
async def test_unicode_escape_cannot_bypass_admin_policy() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Protected encoded name reached downstream")

    async with client_for(upstream) as (client, _):
        response = await client.post(
            "/mcp",
            content=b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"\\u0061dmin_reset_key"}}',
            headers=send_headers(),
        )
    assert response.json()["error"]["code"] == -32001


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,expected_calls", [("get_status", 1), ("admin_reset_key", 0)])
async def test_notifications_never_receive_jsonrpc_responses(
    tool: str, expected_calls: int
) -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    request = rpc("tools/call", name=tool)
    del request["id"]
    async with client_for(upstream) as (client, _):
        response = await client.post("/mcp", json=request, headers=send_headers())
    assert response.status_code == 204 and not response.content
    assert len(seen) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem,status",
    [
        ("connect", 502),
        ("timeout", 504),
        ("http", 502),
        ("html", 502),
        ("wrong_id", 502),
        ("malformed", 502),
        ("redirect", 502),
        ("encoded", 502),
        ("oversized", 502),
    ],
)
async def test_downstream_failures_are_sanitized(
    problem: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        if problem == "connect":
            raise httpx.ConnectError(SENTINEL)
        if problem == "timeout":
            raise httpx.ReadTimeout(SENTINEL)
        if problem == "http":
            return httpx.Response(503, text=SENTINEL)
        if problem == "redirect":
            return httpx.Response(307, headers={"location": "http://internal.invalid/secret"})
        if problem == "html":
            return httpx.Response(200, text=SENTINEL)
        if problem == "wrong_id":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "wrong", "result": SENTINEL})
        if problem == "encoded":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-encoding": "custom"},
                content=b"encoded",
            )
        if problem == "oversized":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b" " * (MAX_RESPONSE_BYTES + 1),
            )
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{" + SENTINEL.encode()
        )

    with caplog.at_level(logging.DEBUG):
        async with client_for(upstream) as (client, _):
            response = await client.post("/mcp", json=rpc(), headers=send_headers())
    assert response.status_code == status
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {
            "code": -32003 if status == 504 else -32002,
            "message": "Downstream timeout" if status == 504 else "Downstream unavailable",
        },
    }
    for secret in (SENTINEL, ADMIN, VIEWER, "Traceback"):
        assert secret not in response.text + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rpc", "tool"])
async def test_downstream_reported_errors_strip_untrusted_details(kind: str) -> None:
    payload = {"jsonrpc": "2.0", "id": "req-1"}
    payload.update(
        {"error": {"code": -32602, "message": SENTINEL, "data": {"trace": SENTINEL}}}
        if kind == "rpc"
        else {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": SENTINEL}],
                "structuredContent": SENTINEL,
            }
        }
    )
    async with client_for(lambda request: httpx.Response(200, json=payload)) as (client, _):
        response = await client.post(
            "/mcp", json=rpc("tools/call", name="demo_error"), headers=send_headers()
        )
    assert response.status_code == 200
    assert SENTINEL not in response.text
    if kind == "rpc":
        assert response.json()["error"] == {"code": -32602, "message": "Downstream RPC error"}
    else:
        assert response.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_cookies_do_not_persist_between_callers() -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "req-1", "result": {}},
            headers={"Set-Cookie": "upstream-session=secret; Path=/"},
        )

    async with client_for(upstream) as (client, app):
        for token in (ADMIN, VIEWER):
            assert (
                await client.post("/mcp", json=rpc(), headers=send_headers(token))
            ).status_code == 200
        assert not list(app.state.downstream.cookies.jar)
    assert len(seen) == 2 and all("cookie" not in request.headers for request in seen)


@pytest.mark.asyncio
async def test_total_timeout_cancels_inflight_request_and_closes_client() -> None:
    cancelled = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(2):
        async with client_for(upstream, timeout=0.01) as (client, app):
            response = await client.post("/mcp", json=rpc(), headers=send_headers())
            assert response.status_code == 504
            assert cancelled.is_set()
            downstream = app.state.downstream
    assert downstream.is_closed


@pytest.mark.asyncio
async def test_caller_cancellation_propagates() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(2), client_for(upstream) as (client, _):
        task = asyncio.create_task(client.post("/mcp", json=rpc(), headers=send_headers()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status", [("oversized", 413), ("content_type", 415), ("encoding", 415)]
)
async def test_http_body_boundaries_do_not_call_downstream(case: str, status: int) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Rejected HTTP input reached downstream")

    headers = send_headers()
    body = json.dumps(rpc()).encode()
    if case == "oversized":
        body = b" " * (gateway.MAX_REQUEST_BYTES + 1)
    elif case == "content_type":
        headers["Content-Type"] = "text/plain"
    else:
        headers["Content-Encoding"] = "gzip"
    async with client_for(upstream) as (client, _):
        response = await client.post("/mcp", content=body, headers=headers)
    assert response.status_code == status


@pytest.mark.asyncio
async def test_authentication_and_denial_logs_do_not_contain_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Rejected input reached downstream")

    with caplog.at_level(logging.INFO):
        async with client_for(upstream) as (client, _):
            await client.post("/mcp", json=rpc(), headers=send_headers("unknown-private-sentinel"))
            await client.post(
                "/mcp",
                json=rpc("tools/call", name="admin_private-sentinel"),
                headers=send_headers(),
            )
    assert "authentication rejected" in caplog.text
    assert "Protected tool call denied" in caplog.text
    for value in (ADMIN, VIEWER, "private-sentinel", "Authorization", "Traceback"):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_exact_request_and_response_size_limits_are_accepted() -> None:
    request_body = json.dumps(rpc()).encode().ljust(gateway.MAX_REQUEST_BYTES)
    response_body = b'{"jsonrpc":"2.0","id":"req-1","result":{}}'.ljust(MAX_RESPONSE_BYTES)

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.content == request_body
        return httpx.Response(
            200, content=response_body, headers={"Content-Type": "application/json"}
        )

    async with client_for(upstream) as (client, _):
        response = await client.post("/mcp", content=request_body, headers=send_headers())
    assert response.status_code == 200 and response.content == response_body


@pytest.mark.asyncio
async def test_slow_request_body_is_bounded_and_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "BODY_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def slow_body() -> AsyncIterator[bytes]:
        try:
            await asyncio.Event().wait()
            yield b"{}"
        finally:
            cancelled.set()

    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Timed-out request body reached downstream")

    async with asyncio.timeout(2), client_for(upstream) as (client, _):
        response = await client.post("/mcp", content=slow_body(), headers=send_headers())
    assert response.status_code == 408
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_unexpected_proxy_error_has_sanitized_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(SENTINEL)

    with caplog.at_level(logging.ERROR):
        async with client_for(upstream) as (client, _):
            response = await client.post("/mcp", json=rpc(), headers=send_headers())
    assert response.status_code == 500
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert SENTINEL not in response.text + caplog.text
    assert "Traceback" not in caplog.text
