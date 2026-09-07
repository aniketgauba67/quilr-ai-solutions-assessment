import json

import pytest

from quilr_assessment.task2_mcp_gateway.rpc import RPCError, inspect_request, inspect_response


@pytest.mark.parametrize("request_id", [0, 42, -1, 1.5, "call-1", "", None])
def test_request_ids_preserved_and_null_is_not_notification(request_id: object) -> None:
    request = inspect_request(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}).encode()
    )
    assert request.request_id == request_id
    assert not request.notification


def test_notification_and_decoded_tool_name() -> None:
    request = inspect_request(
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"\\u0061dmin_reset_key"}}'
    )
    assert request.notification
    assert request.tool_name == "admin_reset_key"


@pytest.mark.parametrize(
    ("payload", "code", "request_id"),
    [
        ([], -32600, None),
        (None, -32600, None),
        ("value", -32600, None),
        ({"jsonrpc": "1.0", "id": "keep", "method": "tools/list"}, -32600, "keep"),
        ({"jsonrpc": "2.0", "id": "keep"}, -32600, "keep"),
        ({"jsonrpc": "2.0", "id": 1, "method": True}, -32600, 1),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/list"}, -32600, None),
        ({"jsonrpc": "2.0", "id": {}, "method": "tools/list"}, -32600, None),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "result": {}}, -32600, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, -32602, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}, -32602, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, -32602, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": None}}, -32602, 1),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}}, -32602, 1),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": ["admin_reset_key"]},
            },
            -32602,
            1,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "normal", "arguments": []},
            },
            -32602,
            1,
        ),
        ({"jsonrpc": "2.0", "id": 1, "method": "other", "params": None}, -32602, 1),
    ],
)
def test_invalid_request_shapes(payload: object, code: int, request_id: object) -> None:
    with pytest.raises(RPCError) as caught:
        inspect_request(json.dumps(payload).encode())
    assert caught.value.code == code
    assert caught.value.request_id == request_id


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b"{} {}",
        b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":1e999,"method":"tools/list"}',
        b'{"jsonrpc":"2.0","id":"\\ud800","method":"tools/list"}',
        b'{"jsonrpc":"2.0","method":"tools/list","method":"tools/call"}',
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_status","name":"admin_reset_key"}}',
    ],
)
def test_invalid_or_ambiguous_json(body: bytes) -> None:
    with pytest.raises(RPCError) as caught:
        inspect_request(body)
    assert caught.value.code == -32700
    assert caught.value.request_id is None


def test_deep_json_is_rejected_cleanly_across_interpreter_recursion_limits() -> None:
    with pytest.raises(RPCError) as caught:
        inspect_request(b"[" * 1100 + b"0" + b"]" * 1100)
    # Either the parser depth limit or the single-object envelope rule rejects it.
    assert caught.value.code in (-32700, -32600)
    assert caught.value.request_id is None


def test_bad_notification_params_remain_silent() -> None:
    with pytest.raises(RPCError) as caught:
        inspect_request(b'{"jsonrpc":"2.0","method":"tools/call","params":{}}')
    assert caught.value.code == -32602
    assert caught.value.notification


@pytest.mark.parametrize("params", [{"extension": True}, [1, 2]])
def test_other_methods_allow_structured_params(params: object) -> None:
    assert (
        inspect_request(
            json.dumps({"jsonrpc": "2.0", "method": "other", "params": params}).encode()
        ).method
        == "other"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "bad"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": []}},
    ],
)
def test_malformed_downstream_envelopes(payload: object) -> None:
    with pytest.raises(ValueError, match="Invalid downstream response"):
        inspect_response(json.dumps(payload).encode(), 1)
