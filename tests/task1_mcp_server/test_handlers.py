import asyncio
import json
import logging
from typing import Any

import pytest
from mcp import Client, MCPError, types

from quilr_assessment.task1_mcp_server import server, tools
from quilr_assessment.task1_mcp_server.schemas import CustomerRecordInput, RefundInput

REFUND = {"customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate payment"}


def test_customer_lookup_and_unknown_customer_are_business_results() -> None:
    found = tools.get_customer_record(CustomerRecordInput(customer_id="CUST-12345"))
    assert found == {
        "status": "found",
        "customer": {
            "customer_id": "CUST-12345",
            "display_name": "Example Customer",
            "plan": "demo",
        },
    }
    assert tools.get_customer_record(CustomerRecordInput(customer_id="CUST-99999")) == {
        "status": "not_found",
        "customer_id": "CUST-99999",
    }


def test_refund_is_deterministic_and_does_not_echo_reason() -> None:
    request = RefundInput.model_validate(REFUND)
    first = tools.trigger_refund(request)
    assert first == tools.trigger_refund(request)
    assert first["status"] == "simulated"
    assert first["refund_id"].startswith("MOCK-")
    assert first["amount"] == 12.5
    assert first["customer_id"] == "CUST-12345"
    assert "reason" not in first
    normalized = RefundInput.model_validate({**REFUND, "reason": "  Duplicate   payment  "})
    assert tools.trigger_refund(normalized) == first
    changed = RefundInput.model_validate({**REFUND, "amount": 13})
    assert tools.trigger_refund(changed)["refund_id"] != first["refund_id"]


def test_refund_unknown_customer_does_not_simulate_a_payment() -> None:
    result = server.execute_tool("trigger_refund", {**REFUND, "customer_id": "CUST-99999"})
    assert not result.is_error
    assert result.structured_content == {"status": "not_found", "customer_id": "CUST-99999"}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("get_customer_record", {"customer_id": "CUST-12345"}), ("trigger_refund", REFUND)],
)
def test_tool_content_matches_structured_result(name: str, arguments: dict[str, object]) -> None:
    result = server.execute_tool(name, arguments)
    assert not result.is_error
    assert len(result.content) == 1 and isinstance(result.content[0], types.TextContent)
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.parametrize(
    ("name", "arguments", "fields"),
    [
        ("get_customer_record", {}, ["customer_id"]),
        ("get_customer_record", {"customer_id": "private-sentinel"}, ["customer_id"]),
        ("get_customer_record", {"customer_id": "CUST-12345", "private-sentinel": 1}, []),
        ("trigger_refund", {**REFUND, "amount": True}, ["amount"]),
        ("trigger_refund", {**REFUND, "reason": "short"}, ["reason"]),
        ("trigger_refund", None, []),
    ],
)
def test_validation_prevents_execution_and_exposes_only_known_field_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    arguments: object,
    fields: list[str],
) -> None:
    def must_not_execute(*args: object) -> None:
        pytest.fail("Invalid arguments reached business execution")

    monkeypatch.setattr(tools, "get_customer_record", must_not_execute)
    monkeypatch.setattr(tools, "trigger_refund", must_not_execute)
    with caplog.at_level(logging.WARNING), pytest.raises(MCPError) as caught:
        server.execute_tool(name, arguments)
    assert caught.value.code == -32602
    assert caught.value.message == "Invalid params"
    assert caught.value.data == {"fields": fields}
    assert "private-sentinel" not in caplog.text + str(caught.value.error)


@pytest.mark.asyncio
async def test_sdk_discovery_and_protocol_errors() -> None:
    async with asyncio.timeout(5), Client(server.create_server()) as client:
        listing = await client.list_tools()
        advertised = {tool.name: tool.input_schema for tool in listing.tools}
        assert set(advertised) == {"get_customer_record", "trigger_refund"}
        for schema in advertised.values():
            assert schema["additionalProperties"] is False
            assert "customer_id" in schema["required"]
        assert set(advertised["trigger_refund"]["required"]) == {"customer_id", "amount", "reason"}
        assert advertised["trigger_refund"]["properties"]["amount"]["exclusiveMinimum"] == 0
        assert advertised["trigger_refund"]["properties"]["reason"]["minLength"] == 10
        for name, arguments in [
            ("get_customer_record", {}),
            ("trigger_refund", {**REFUND, "amount": 0}),
            ("unknown-private-sentinel", {}),
        ]:
            with pytest.raises(MCPError) as caught:
                await client.call_tool(name, arguments)
            assert caught.value.code == -32602
            assert "unknown-private-sentinel" not in str(caught.value.error)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tool", "discovery"])
async def test_unexpected_failure_is_sanitized_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    sentinel = "private-sentinel /private/server.py API_KEY=secret"

    def fail(*args: object, **kwargs: object) -> Any:
        raise RuntimeError(sentinel)

    if operation == "tool":
        monkeypatch.setattr(tools, "get_customer_record", fail)
    else:
        monkeypatch.setattr(CustomerRecordInput, "model_json_schema", fail)
    async with asyncio.timeout(5), Client(server.create_server(), mode="legacy") as client:
        with caplog.at_level(logging.ERROR), pytest.raises(MCPError) as caught:
            if operation == "tool":
                await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
            else:
                await client.list_tools()
        assert caught.value.code == -32603
        assert caught.value.message == "Internal error"
        assert caught.value.data is None
    assert sentinel not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_internal_error() -> None:
    async def cancel(context: object) -> Any:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await server.sanitize_errors(None, cancel)  # type: ignore[arg-type]
