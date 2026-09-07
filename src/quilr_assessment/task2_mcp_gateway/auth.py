"""Opaque demo credentials resolve roles; tool permissions live in policy.py."""

import re
from hmac import compare_digest
from typing import Literal

Role = Literal["admin", "viewer"]
TOKEN_PATTERN = r"[A-Za-z0-9._~+/-]+=*"


class AuthenticationError(Exception):
    pass


def parse_bearer(header: str) -> str:
    if len(header) > 4103:
        raise AuthenticationError("Authentication required")
    match = re.fullmatch(rf"(?i:Bearer) +({TOKEN_PATTERN})", header)
    if match is None:
        raise AuthenticationError("Authentication required")
    return match[1]


def authenticate(headers: list[str], *, admin_token: str, viewer_token: str) -> Role:
    if len(headers) != 1:
        raise AuthenticationError("Authentication required")
    token = parse_bearer(headers[0]).encode("ascii")
    admin = compare_digest(token, admin_token.encode("ascii"))
    viewer = compare_digest(token, viewer_token.encode("ascii"))
    if admin:
        return "admin"
    if viewer:
        return "viewer"
    raise AuthenticationError("Authentication required")
