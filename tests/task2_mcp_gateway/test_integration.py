"""Run both documented Uvicorn factories and send real loopback HTTP requests."""

import asyncio
import os
import signal
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

VIEWER_TOKEN = "integration-viewer-credential"
ADMIN_TOKEN = "integration-admin-credential"


@asynccontextmanager
async def _serve(factory: str, environment: dict[str, str]) -> AsyncIterator[str]:
    # Reserve the socket until Uvicorn inherits it, avoiding a free-port race.
    with socket.socket() as listener, tempfile.TemporaryFile() as logs:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            factory,
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--fd",
            str(listener.fileno()),
            "--no-access-log",
            env={**os.environ, **environment},
            pass_fds=(listener.fileno(),),
            stdout=logs,
            stderr=logs,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                    while True:
                        if process.returncode is not None:
                            logs.seek(0)
                            pytest.fail(f"Uvicorn exited before startup: {logs.read().decode()}")
                        try:
                            response = await client.get(f"{base_url}/mcp")
                            assert response.status_code == 405
                            break
                        except httpx.TransportError:
                            await asyncio.sleep(0.025)
            yield base_url
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
            logs.seek(0)
            captured = logs.read().decode()
            assert VIEWER_TOKEN not in captured
            assert ADMIN_TOKEN not in captured
            assert "Traceback" not in captured
        # Uvicorn re-raises SIGTERM after completing its lifespan shutdown.
        assert process.returncode in (0, -signal.SIGTERM), captured
        assert "Application shutdown complete" in captured


def _request(request_id: int | str, method: str, name: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if name is not None:
        body["params"] = {"name": name, "arguments": {}}
    return body


async def test_real_http_gateway_forwards_allowed_calls_and_rejects_viewer_admin() -> None:
    async with asyncio.timeout(30):
        async with _serve("quilr_assessment.mocks.mcp:create_app", {}) as downstream:
            environment = {
                "QUILR_MCP_DOWNSTREAM_URL": f"{downstream}/mcp",
                "QUILR_VIEWER_TOKEN": VIEWER_TOKEN,
                "QUILR_ADMIN_TOKEN": ADMIN_TOKEN,
            }
            async with _serve(
                "quilr_assessment.task2_mcp_gateway.app:create_app", environment
            ) as gateway:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    unauthenticated = await client.post(
                        f"{gateway}/mcp", json=_request(0, "tools/list")
                    )
                    assert unauthenticated.status_code == 401
                    assert unauthenticated.headers["www-authenticate"].startswith("Bearer")

                    viewer_headers = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
                    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
                    listed_body = _request("discovery", "tools/list")
                    direct_list = await client.post(f"{downstream}/mcp", json=listed_body)
                    for headers in (viewer_headers, admin_headers):
                        listed = await client.post(
                            f"{gateway}/mcp", headers=headers, json=listed_body
                        )
                        assert listed.status_code == direct_list.status_code == 200
                        assert listed.content == direct_list.content
                        assert "admin_reset_key" in {
                            tool["name"] for tool in listed.json()["result"]["tools"]
                        }

                    normal_body = _request(1, "tools/call", "get_status")
                    direct_normal = await client.post(f"{downstream}/mcp", json=normal_body)
                    for headers in (viewer_headers, admin_headers):
                        normal = await client.post(
                            f"{gateway}/mcp", headers=headers, json=normal_body
                        )
                        assert normal.status_code == direct_normal.status_code == 200
                        assert normal.content == direct_normal.content
                        assert normal.json()["result"]["structuredContent"]["status"] == "ok"

                    protected_body = _request("protected-42", "tools/call", "admin_reset_key")
                    forbidden = await client.post(
                        f"{gateway}/mcp", headers=viewer_headers, json=protected_body
                    )
                    assert forbidden.status_code == 200
                    assert forbidden.json() == {
                        "jsonrpc": "2.0",
                        "id": "protected-42",
                        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
                    }

                    direct_admin = await client.post(f"{downstream}/mcp", json=protected_body)
                    allowed_admin = await client.post(
                        f"{gateway}/mcp", headers=admin_headers, json=protected_body
                    )
                    assert allowed_admin.status_code == direct_admin.status_code == 200
                    assert allowed_admin.content == direct_admin.content
                    assert (
                        allowed_admin.json()["result"]["structuredContent"]["status"] == "simulated"
                    )
