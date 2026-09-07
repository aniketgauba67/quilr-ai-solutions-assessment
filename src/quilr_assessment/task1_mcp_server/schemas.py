"""The tool schemas are also the runtime validation boundary."""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def normalize_reason(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


CustomerId = Annotated[
    str,
    Field(min_length=10, max_length=10, pattern=r"^CUST-[0-9]{5}$"),
]


class CustomerRecordInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    customer_id: CustomerId


class RefundInput(CustomerRecordInput):
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[
        str,
        Field(
            min_length=10,
            description="At least 10 characters after trimming and collapsing whitespace runs.",
        ),
        BeforeValidator(normalize_reason),
    ]
