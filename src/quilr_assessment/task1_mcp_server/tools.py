"""Deterministic synthetic results; no payment service or persistent state."""

import hashlib
import json
from typing import Literal, TypedDict

from .schemas import CustomerRecordInput, RefundInput


class CustomerRecord(TypedDict):
    customer_id: str
    display_name: str
    plan: str


class CustomerFound(TypedDict):
    status: Literal["found"]
    customer: CustomerRecord


class CustomerNotFound(TypedDict):
    status: Literal["not_found"]
    customer_id: str


class SimulatedRefund(TypedDict):
    status: Literal["simulated"]
    refund_id: str
    customer_id: str
    amount: float


def get_customer_record(request: CustomerRecordInput) -> CustomerFound | CustomerNotFound:
    if request.customer_id != "CUST-12345":
        return {"status": "not_found", "customer_id": request.customer_id}
    return {
        "status": "found",
        "customer": {
            "customer_id": request.customer_id,
            "display_name": "Example Customer",
            "plan": "demo",
        },
    }


def trigger_refund(request: RefundInput) -> SimulatedRefund | CustomerNotFound:
    if get_customer_record(request)["status"] == "not_found":
        return {"status": "not_found", "customer_id": request.customer_id}
    # An input-derived identifier makes retries repeatable without a refund ledger.
    canonical = json.dumps(request.model_dump(), sort_keys=True, allow_nan=False)
    identifier = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return {
        "status": "simulated",
        "refund_id": f"MOCK-{identifier}",
        "customer_id": request.customer_id,
        "amount": request.amount,
    }
