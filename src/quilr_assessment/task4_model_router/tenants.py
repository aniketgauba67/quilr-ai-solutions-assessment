"""Tenant identity for metering: bearer keys become salted fingerprints, never rows."""

import re
from hashlib import blake2b
from hmac import compare_digest

TOKEN_PATTERN = r"[A-Za-z0-9._~+/-]+=*"
MAX_HEADER_CHARS = 4103
FINGERPRINT_BYTES = 32
_PERSON = b"quilr-tenant-v1"


class TenantError(Exception):
    """A fixed authentication failure; it never repeats the presented credential."""

    def __init__(self) -> None:
        super().__init__("A valid tenant API key is required")


def parse_bearer(header: str) -> str:
    if len(header) > MAX_HEADER_CHARS:
        raise TenantError()
    match = re.fullmatch(rf"(?i:Bearer) +({TOKEN_PATTERN})", header)
    if match is None:
        raise TenantError()
    return match[1]


def fingerprint(api_key: str, *, pepper: bytes = b"") -> str:
    """Keyed when a pepper is configured, so stored rows resist offline key guessing."""
    digest = blake2b(
        api_key.encode("utf-8"),
        digest_size=FINGERPRINT_BYTES,
        key=pepper[:64],
        person=_PERSON,
    )
    return digest.hexdigest()


def resolve_tenant(
    headers: list[str], *, pepper: bytes = b"", allowlist: tuple[str, ...] | None = None
) -> str:
    """Return the fingerprint used for quota accounting; the raw key is discarded."""
    if len(headers) != 1:
        raise TenantError()
    identity = fingerprint(parse_bearer(headers[0]), pepper=pepper)
    if allowlist is None:
        return identity
    if not any(compare_digest(identity, known) for known in allowlist):
        raise TenantError()
    return identity
