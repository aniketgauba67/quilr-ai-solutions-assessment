import math

import pytest
from pydantic import ValidationError

from quilr_assessment.task1_mcp_server.schemas import CustomerRecordInput, RefundInput

REFUND = {"customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate payment"}


@pytest.mark.parametrize("customer_id", ["CUST-12345", "CUST-00000", "CUST-99999"])
def test_valid_customer_ids(customer_id: str) -> None:
    assert CustomerRecordInput(customer_id=customer_id).customer_id == customer_id


@pytest.mark.parametrize(
    "customer_id",
    [
        "12345",
        "USER-12345",
        "cust-12345",
        "CUST-123",
        "CUST-123456",
        "CUST-ABCDE",
        "CUST-12A45",
        "",
        "          ",
        " CUST-12345",
        "CUST-12345 ",
        "CUST-12345\n",
        "CUST-１２３４５",
        "CUST-١٢٣٤٥",
        12345,
        True,
        None,
        [],
        {},
        b"CUST-12345",
    ],
)
@pytest.mark.parametrize("model", [CustomerRecordInput, RefundInput])
def test_invalid_customer_ids(model: type[CustomerRecordInput], customer_id: object) -> None:
    values = REFUND if model is RefundInput else {}
    with pytest.raises(ValidationError):
        model.model_validate({**values, "customer_id": customer_id})


@pytest.mark.parametrize("model", [CustomerRecordInput, RefundInput])
def test_missing_customer_id(model: type[CustomerRecordInput]) -> None:
    values = {key: value for key, value in REFUND.items() if key != "customer_id"}
    with pytest.raises(ValidationError):
        model.model_validate(values if model is RefundInput else {})


@pytest.mark.parametrize("amount", [0.01, 1, 12.5, 1e100])
def test_positive_finite_json_numbers(amount: float) -> None:
    result = RefundInput.model_validate({**REFUND, "amount": amount})
    assert result.amount == amount
    assert isinstance(result.amount, float)


@pytest.mark.parametrize(
    "amount",
    [
        0,
        -0.0,
        -0.01,
        -1,
        math.nan,
        math.inf,
        -math.inf,
        "1",
        "12.50",
        "oops",
        True,
        False,
        None,
        [],
        {},
        10**400,
    ],
)
def test_invalid_amounts(amount: object) -> None:
    with pytest.raises(ValidationError):
        RefundInput.model_validate({**REFUND, "amount": amount})


@pytest.mark.parametrize(
    ("reason", "normalized"),
    [
        ("Valid text", "Valid text"),
        ("0123456789", "0123456789"),
        (" \tDuplicate  payment\n", "Duplicate payment"),
        ("Duplicate\u2003payment", "Duplicate payment"),
    ],
)
def test_reason_boundary_and_whitespace_normalization(reason: str, normalized: str) -> None:
    assert RefundInput.model_validate({**REFUND, "reason": reason}).reason == normalized


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "123456789",
        "          ",
        "\n\t \u2003",
        "  too short  ",
        "a          b",
        1234567890,
        True,
        None,
        [],
        {},
        b"Duplicate payment",
    ],
)
def test_invalid_reasons(reason: object) -> None:
    with pytest.raises(ValidationError):
        RefundInput.model_validate({**REFUND, "reason": reason})


@pytest.mark.parametrize("field", ["amount", "reason"])
def test_missing_refund_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        RefundInput.model_validate({key: value for key, value in REFUND.items() if key != field})


@pytest.mark.parametrize("model", [CustomerRecordInput, RefundInput])
def test_extra_fields_forbidden(model: type[CustomerRecordInput]) -> None:
    values = REFUND if model is RefundInput else {"customer_id": "CUST-12345"}
    with pytest.raises(ValidationError):
        model.model_validate({**values, "unexpected": "value"})


@pytest.mark.parametrize("value", [None, [], "customer_id=CUST-12345", True])
def test_input_requires_an_object(value: object) -> None:
    with pytest.raises(ValidationError):
        CustomerRecordInput.model_validate(value)
