import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from quilr_assessment.mocks.mcp import MAX_BODY_BYTES, create_app


@pytest.fixture
def mock_app() -> FastAPI:
    return create_app()


@pytest_asyncio.fixture
async def client(mock_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_mock_lists_normal_protected_and_failing_tools(client: httpx.AsyncClient) -> None:
    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert response.json()["id"] == 1
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "get_status",
        "admin_reset_key",
        "demo_error",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("get_status", {"status": "ok", "service": "mock-mcp"}),
        ("admin_reset_key", {"status": "simulated", "operation": "reset_key"}),
    ],
)
async def test_mock_calls_are_deterministic(
    client: httpx.AsyncClient, name: str, expected: dict[str, str]
) -> None:
    request = {"jsonrpc": "2.0", "id": "same-id", "method": "tools/call", "params": {"name": name}}
    first = await client.post("/mcp", json=request)
    second = await client.post("/mcp", json=request)
    assert first.content == second.content
    payload = first.json()
    assert payload["id"] == "same-id"
    assert payload["result"]["structuredContent"] == expected
    assert json.loads(payload["result"]["content"][0]["text"]) == expected
    assert payload["result"]["isError"] is False


@pytest.mark.asyncio
async def test_mock_tool_failure_is_a_clean_business_result(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "demo_error"}},
    )
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": "Mock tool execution failed"}],
            "isError": True,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code", "expected_id"),
    [
        ([], -32600, None),
        ({"jsonrpc": "2.0", "id": 5}, -32600, None),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/list"}, -32600, None),
        ({"jsonrpc": "2.0", "id": 5, "method": "other"}, -32601, 5),
        ({"jsonrpc": "2.0", "id": 5, "method": "tools/call"}, -32602, 5),
        ({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": []}, -32602, 5),
        (
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "unknown"}},
            -32602,
            5,
        ),
        (
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 123}},
            -32602,
            5,
        ),
    ],
)
async def test_mock_rejects_malformed_calls_cleanly(
    client: httpx.AsyncClient, payload: object, code: int, expected_id: object
) -> None:
    response = await client.post("/mcp", json=payload)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code
    assert response.json()["id"] == expected_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"broken",
        b"\xff",
        b'{"jsonrpc":"2.0","id":NaN}',
        b'{"jsonrpc":"2.0","id":1e400}',
        b'{"jsonrpc":"2.0","id":"\\ud800"}',
    ],
)
async def test_mock_rejects_invalid_json(client: httpx.AsyncClient, body: bytes) -> None:
    response = await client.post("/mcp", content=body)
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }


@pytest.mark.asyncio
async def test_mock_notification_null_id_and_request_counter(
    client: httpx.AsyncClient, mock_app: FastAPI
) -> None:
    assert mock_app.state.request_count == 0
    notification = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"})
    assert notification.status_code == 204
    assert notification.content == b""
    request = await client.post("/mcp", json={"jsonrpc": "2.0", "id": None, "method": "tools/list"})
    assert request.status_code == 200
    assert request.json()["id"] is None
    await client.post("/mcp", content="malformed")
    assert mock_app.state.request_count == 3


@pytest.mark.asyncio
async def test_mock_preserves_fractional_numeric_id(client: httpx.AsyncClient) -> None:
    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"})
    assert response.json()["id"] == 1.5


@pytest.mark.asyncio
async def test_mock_bounds_body_size(client: httpx.AsyncClient) -> None:
    response = await client.post("/mcp", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json() == {"error": "Request too large"}
