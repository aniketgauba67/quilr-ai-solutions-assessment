"""Official MCP handlers with explicit validation and sanitized protocol errors."""

import json
import logging

from mcp import MCPError, types
from mcp.server import ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from . import tools
from .schemas import CustomerRecordInput, RefundInput

logger = logging.getLogger(__name__)


async def sanitize_errors(context: ServerRequestContext, call_next: CallNext) -> HandlerResult:
    try:
        return await call_next(context)
    except MCPError as exc:
        # SDK-generated errors can attach caller-controlled method names in data.
        if exc.code == types.METHOD_NOT_FOUND:
            raise MCPError(types.METHOD_NOT_FOUND, "Method not found") from None
        raise
    except ValidationError:
        raise MCPError(types.INVALID_PARAMS, "Invalid params") from None
    except Exception:
        logger.error("Task 1 request failed")
        raise MCPError(types.INTERNAL_ERROR, "Internal error") from None


async def list_tools(
    context: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="get_customer_record",
                description="Look up a synthetic customer record; unknown IDs return not_found.",
                input_schema=CustomerRecordInput.model_json_schema(),
            ),
            types.Tool(
                name="trigger_refund",
                description="Simulate a refund for a synthetic customer. No payment is performed.",
                input_schema=RefundInput.model_json_schema(),
            ),
        ]
    )


def execute_tool(name: str, arguments: object) -> types.CallToolResult:
    if name not in {"get_customer_record", "trigger_refund"}:
        raise MCPError(types.INVALID_PARAMS, "Invalid params", {"detail": "Unknown tool"})

    model = CustomerRecordInput if name == "get_customer_record" else RefundInput
    try:
        request = model.model_validate(arguments)
    except ValidationError as exc:
        # Only schema-owned field names cross the boundary, never values or extra keys.
        fields = sorted(
            {
                str(error["loc"][0])
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
                if error["loc"] and error["loc"][0] in model.model_fields
            }
        )
        logger.warning("Rejected invalid tool arguments")
        raise MCPError(types.INVALID_PARAMS, "Invalid params", {"fields": fields}) from None

    try:
        if isinstance(request, RefundInput):
            payload = tools.trigger_refund(request)
        else:
            payload = tools.get_customer_record(request)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, allow_nan=False))],
            structured_content=payload,
        )
    except Exception:
        logger.error("Task 1 tool execution failed")
        raise MCPError(types.INTERNAL_ERROR, "Internal error") from None


async def call_tool(
    context: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    return execute_tool(params.name, params.arguments)


def create_server() -> Server:
    server = Server(
        "quilr-task1",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.middleware.append(sanitize_errors)
    return server
