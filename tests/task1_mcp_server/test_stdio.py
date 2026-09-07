"""Exercise the installed entry point over real stdin/stdout pipes."""

import asyncio
import json
import sys
from typing import Any

import pytest
from mcp import MCPError
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SERVER_MODULE = "quilr_assessment.task1_mcp_server"
IO_TIMEOUT = 5.0


async def test_official_client_initializes_discovers_and_calls_tools() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE],
    )
    async with asyncio.timeout(20):
        async with Client(parameters, mode="legacy", read_timeout_seconds=IO_TIMEOUT) as client:
            discovered = await client.list_tools()
            assert {tool.name for tool in discovered.tools} == {
                "get_customer_record",
                "trigger_refund",
            }

            customer = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
            assert not customer.is_error
            assert customer.structured_content["customer"]["customer_id"] == "CUST-12345"

            refund = await client.call_tool(
                "trigger_refund",
                {
                    "customer_id": "CUST-12345",
                    "amount": 12.5,
                    "reason": "Duplicate payment",
                },
            )
            assert not refund.is_error
            assert refund.structured_content["status"] == "simulated"
            assert refund.structured_content["amount"] == 12.5
            assert refund.structured_content["refund_id"].startswith("MOCK-")
            assert "reason" not in refund.structured_content
            assert json.loads(refund.content[0].text) == refund.structured_content

            unknown = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
            assert not unknown.is_error
            assert unknown.structured_content == {
                "status": "not_found",
                "customer_id": "CUST-99999",
            }

            for name, arguments in (
                ("get_customer_record", {}),
                ("get_customer_record", {"customer_id": "cust-12345"}),
                (
                    "trigger_refund",
                    {"customer_id": "CUST-12345", "amount": True, "reason": "Duplicate payment"},
                ),
                ("unknown_tool", {}),
            ):
                with pytest.raises(MCPError) as rejected:
                    await client.call_tool(name, arguments)
                assert rejected.value.code == -32602
                assert rejected.value.message == "Invalid params"

            # An invalid call must not terminate the session.
            assert len((await client.list_tools()).tools) == 2


async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message, allow_nan=False) + "\n").encode())
    await asyncio.wait_for(process.stdin.drain(), timeout=IO_TIMEOUT)


async def _receive(process: asyncio.subprocess.Process, request_id: int | str) -> dict[str, Any]:
    assert process.stdout is not None
    raw = await asyncio.wait_for(process.stdout.readline(), timeout=IO_TIMEOUT)
    assert raw, "The MCP process exited before responding"
    message = json.loads(raw)
    assert message["jsonrpc"] == "2.0"
    assert message["id"] == request_id
    assert ("result" in message) != ("error" in message)
    return message


async def _close(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    if process.stdin is not None:
        process.stdin.close()
    try:
        return await asyncio.wait_for(process.communicate(), timeout=IO_TIMEOUT)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.communicate(), timeout=IO_TIMEOUT)
        pytest.fail("The MCP process failed to stop after stdin closed")


async def _initialize(process: asyncio.subprocess.Process) -> None:
    await _send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "task1-wire-test", "version": "1.0"},
            },
        },
    )
    initialized = await _receive(process, 0)
    assert "tools" in initialized["result"]["capabilities"]
    await _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})


async def test_wire_errors_preserve_ids_and_stdout_contains_only_protocol() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        SERVER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _initialize(process)

        await _send(process, {"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        listed = await _receive(process, "list")
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "get_customer_record",
            "trigger_refund",
        }

        await _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "refund",
                "method": "tools/call",
                "params": {
                    "name": "trigger_refund",
                    "arguments": {
                        "customer_id": "CUST-12345",
                        "amount": 20,
                        "reason": "Private refund reason sentinel",
                    },
                },
            },
        )
        refund = await _receive(process, "refund")
        assert refund["result"]["structuredContent"]["status"] == "simulated"
        assert "Private refund reason sentinel" not in json.dumps(refund)

        invalid_params = [
            {"name": "get_customer_record", "arguments": {}},
            {
                "name": "get_customer_record",
                "arguments": {"customer_id": "sensitive-customer-sentinel"},
            },
            {
                "name": "get_customer_record",
                "arguments": {"customer_id": "CUST-12345", "unexpected-secret-sentinel": True},
            },
            {
                "name": "trigger_refund",
                "arguments": {
                    "customer_id": "CUST-12345",
                    "amount": "secret-amount-sentinel",
                    "reason": "Private refund reason sentinel",
                },
            },
            {"name": "get_customer_record", "arguments": ["malformed-arguments-sentinel"]},
            {"name": "unknown-tool-sentinel", "arguments": {}},
        ]
        for request_id, params in enumerate(invalid_params, start=1):
            await _send(
                process,
                {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params},
            )
            response = await _receive(process, request_id)
            assert response["error"]["code"] == -32602
            assert response["error"]["message"] == "Invalid params"
            serialized = json.dumps(response)
            assert "sentinel" not in serialized
            assert "Traceback" not in serialized
            assert "site-packages" not in serialized

        await _send(process, {"jsonrpc": "2.0", "id": "unknown", "method": "unknown/method"})
        unknown_method = await _receive(process, "unknown")
        assert unknown_method["error"]["code"] == -32601
    finally:
        remaining_stdout, stderr = await _close(process)

    assert process.returncode == 0
    assert remaining_stdout == b"", "Unexpected stdout after the final protocol response"
    logs = stderr.decode()
    assert "Starting Task 1 MCP server" in logs
    assert "Rejected invalid tool arguments" in logs
    assert "sentinel" not in logs
    assert "CUST-12345" not in logs
    assert "Traceback" not in logs
    assert "site-packages" not in logs


async def test_sdk_drops_malformed_frames_and_recovers_without_leaking_payloads() -> None:
    """SDK 2.1.1 drops bad frames; this does not claim parse-error responses."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        SERVER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(b'{"malformed-json-sentinel":\n')
        await asyncio.wait_for(process.stdin.drain(), timeout=IO_TIMEOUT)
        await _initialize(process)

        # Invalid tool arguments would log a rejection if these envelopes reached the handler.
        for version, request_id in (("1.0", "invalid-version-sentinel"), ("2.0", {})):
            await _send(
                process,
                {
                    "jsonrpc": version,
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "trigger_refund",
                        "arguments": {"reason": "invalid-envelope-sentinel"},
                    },
                },
            )
        await _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "recovered",
                "method": "tools/call",
                "params": {
                    "name": "get_customer_record",
                    "arguments": {"customer_id": "CUST-12345"},
                },
            },
        )
        recovered = await _receive(process, "recovered")
        assert recovered["result"]["structuredContent"]["customer"]["customer_id"] == "CUST-12345"
    finally:
        remaining_stdout, stderr = await _close(process)

    assert process.returncode == 0
    assert remaining_stdout == b""
    logs = stderr.decode()
    assert "Starting Task 1 MCP server" in logs
    assert "Rejected invalid tool arguments" not in logs
    assert "sentinel" not in logs
    assert "Traceback" not in logs
    assert "site-packages" not in logs
