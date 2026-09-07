"""Stable outward error envelope; upstream text never reaches this layer."""

from typing import Any

RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
QUOTA_STORAGE_UNAVAILABLE = "QUOTA_STORAGE_UNAVAILABLE"
INVALID_REQUEST = "INVALID_REQUEST"
UNAUTHORIZED = "UNAUTHORIZED"
INTERNAL_ERROR = "INTERNAL_ERROR"

MESSAGES = {
    RATE_LIMIT_EXCEEDED: "Token quota exceeded for this API key. Retry after the current window.",
    UPSTREAM_UNAVAILABLE: "Unable to complete the request using the available model providers.",
    QUOTA_STORAGE_UNAVAILABLE: "Quota accounting is unavailable; the request was not sent.",
    INVALID_REQUEST: "Invalid completion request.",
    UNAUTHORIZED: "A valid tenant API key is required.",
    INTERNAL_ERROR: "The gateway could not process this request.",
}
STATUS = {
    RATE_LIMIT_EXCEEDED: 429,
    UPSTREAM_UNAVAILABLE: 502,
    QUOTA_STORAGE_UNAVAILABLE: 503,
    INVALID_REQUEST: 422,
    UNAUTHORIZED: 401,
    INTERNAL_ERROR: 500,
}


class GatewayError(Exception):
    """Carries only a known code; callers never attach upstream detail to it."""

    def __init__(self, code: str) -> None:
        if code not in MESSAGES:
            raise ValueError("Unknown gateway error code")
        self.code = code
        self.status = STATUS[code]
        self.message = MESSAGES[code]
        super().__init__(code)

    def payload(self) -> dict[str, Any]:
        return error_payload(self.code)


def error_payload(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": MESSAGES[code]}}
