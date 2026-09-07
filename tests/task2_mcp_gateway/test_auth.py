from hmac import compare_digest

import pytest
from pydantic import ValidationError

from quilr_assessment.task2_mcp_gateway import auth
from quilr_assessment.task2_mcp_gateway.auth import AuthenticationError, authenticate, parse_bearer
from quilr_assessment.task2_mcp_gateway.config import Settings, from_environment
from quilr_assessment.task2_mcp_gateway.policy import may_call_tool

ADMIN = "Admin-test-sentinel"
VIEWER = "Viewer-test-sentinel"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
@pytest.mark.parametrize(("token", "role"), [(ADMIN, "admin"), (VIEWER, "viewer")])
def test_authenticated_role_and_case_insensitive_scheme(scheme: str, token: str, role: str) -> None:
    assert authenticate([f"{scheme} {token}"], admin_token=ADMIN, viewer_token=VIEWER) == role


@pytest.mark.parametrize("token", ["plain", "AbC09._~+/-==", "x" * 4096])
def test_bearer_credential_alphabet_and_length_boundary(token: str) -> None:
    assert parse_bearer(f"Bearer {token}") == token


def test_bearer_allows_multiple_ascii_spaces_between_scheme_and_token() -> None:
    assert parse_bearer(f"Bearer   {ADMIN}") == ADMIN


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer    ",
        f"Basic {ADMIN}",
        f"Bearer{ADMIN}",
        f"Bearer\t{ADMIN}",
        f" Bearer {ADMIN}",
        f"Bearer {ADMIN} ",
        f"Bearer {ADMIN}\n",
        f"Bearer {ADMIN}\r\nInjected: value",
        f"Bearer {ADMIN} {VIEWER}",
        f"Bearer {ADMIN}, Bearer {VIEWER}",
        "Bearer café",
        "Bearer =",
        "Bearer a=b",
        "Bearer !",
        "Bearer " + "x" * 4097,
    ],
)
def test_malformed_bearer_header_is_rejected_without_echoing_input(header: str) -> None:
    with pytest.raises(AuthenticationError) as caught:
        parse_bearer(header)
    assert str(caught.value) == "Authentication required"
    assert ADMIN not in str(caught.value)
    assert VIEWER not in str(caught.value)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [f"Bearer {ADMIN}", f"Bearer {ADMIN}"],
        [f"Bearer {ADMIN}", f"Bearer {VIEWER}"],
        [f"Bearer {VIEWER}", f"Bearer {ADMIN}"],
        ["Bearer unknown-test-sentinel"],
        [f"Bearer {ADMIN.lower()}"],
        [f"Bearer {VIEWER.upper()}"],
    ],
)
def test_missing_duplicate_and_unknown_credentials_are_rejected(headers: list[str]) -> None:
    with pytest.raises(AuthenticationError, match="^Authentication required$"):
        authenticate(headers, admin_token=ADMIN, viewer_token=VIEWER)


@pytest.mark.parametrize("token", [ADMIN, VIEWER, "unknown-test-sentinel"])
def test_role_lookup_compares_both_credentials_without_short_circuit(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def record_comparison(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(auth, "compare_digest", record_comparison)
    try:
        authenticate([f"Bearer {token}"], admin_token=ADMIN, viewer_token=VIEWER)
    except AuthenticationError:
        assert token not in (ADMIN, VIEWER)
    assert comparisons == [(token.encode(), ADMIN.encode()), (token.encode(), VIEWER.encode())]


@pytest.mark.parametrize("name", ["admin_", "admin_reset_key", "admin_rotate_secret", "admin_🔑"])
def test_exact_admin_prefix_requires_admin(name: str) -> None:
    assert may_call_tool("admin", name) is True
    assert may_call_tool("viewer", name) is False


@pytest.mark.parametrize(
    "name",
    ["get_status", "admin", "Admin_reset_key", "ADMIN_reset_key", " admin_reset_key", "аdmin_key"],
)
def test_policy_does_not_invent_restrictions_beyond_exact_prefix(name: str) -> None:
    assert may_call_tool("admin", name) is True
    assert may_call_tool("viewer", name) is True


@pytest.mark.parametrize("token", ["", " ", "non ascii é", "café", "a\nb", "a=b", "x" * 4097])
@pytest.mark.parametrize("role", ["admin_token", "viewer_token"])
def test_configuration_rejects_invalid_tokens_without_displaying_them(
    token: str, role: str
) -> None:
    values = {"admin_token": ADMIN, "viewer_token": VIEWER, role: token}
    with pytest.raises(ValidationError) as caught:
        Settings(**values)
    assert "input_value=" not in str(caught.value)
    assert ADMIN not in str(caught.value)
    assert VIEWER not in str(caught.value)


def test_configuration_rejects_duplicate_tokens_without_leaking_them() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(admin_token=ADMIN, viewer_token=ADMIN)
    assert "distinct" in str(caught.value)
    assert ADMIN not in str(caught.value)


def test_configuration_hides_credentials_in_repr_and_serialization() -> None:
    settings = Settings(admin_token=ADMIN, viewer_token=VIEWER)
    for rendered in [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ]:
        assert ADMIN not in rendered
        assert VIEWER not in rendered
    assert "admin_token" not in settings.model_dump()
    assert "viewer_token" not in settings.model_dump()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9000/mcp", "https://example.test/mcp", "http://[::1]:9000/mcp"]
)
def test_configuration_accepts_http_downstreams(url: str) -> None:
    assert (
        str(Settings(admin_token=ADMIN, viewer_token=VIEWER, downstream_url=url).downstream_url)
        == url
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/mcp",
        "file:///private/server.py",
        "example.test/mcp",
        "http://user:private-url-sentinel@example.test/mcp",
        "http://user@example.test/mcp",
        "http://example.test/mcp?key=private-url-sentinel",
        "http://example.test/mcp#private-url-sentinel",
    ],
)
def test_configuration_rejects_unsupported_or_credential_bearing_urls(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(admin_token=ADMIN, viewer_token=VIEWER, downstream_url=url)
    assert "private-url-sentinel" not in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1, 30.001, float("nan"), float("inf"), float("-inf")])
def test_configuration_requires_finite_bounded_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(admin_token=ADMIN, viewer_token=VIEWER, timeout_seconds=timeout)


@pytest.mark.parametrize("timeout", [0.001, 30])
def test_configuration_accepts_timeout_boundaries(timeout: float) -> None:
    settings = Settings(admin_token=ADMIN, viewer_token=VIEWER, timeout_seconds=timeout)
    assert settings.timeout_seconds == timeout


@pytest.mark.parametrize("present_role", [None, "ADMIN", "VIEWER"])
def test_environment_factory_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch, present_role: str | None
) -> None:
    monkeypatch.delenv("QUILR_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("QUILR_VIEWER_TOKEN", raising=False)
    if present_role is not None:
        monkeypatch.setenv(f"QUILR_{present_role}_TOKEN", "environment-secret-sentinel")
    with pytest.raises(ValueError) as caught:
        from_environment()
    assert str(caught.value) == (
        "Invalid Task 2 configuration; check the documented environment settings"
    )
    assert caught.value.__suppress_context__ is True


def test_environment_factory_reads_explicit_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUILR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("QUILR_VIEWER_TOKEN", VIEWER)
    monkeypatch.setenv("QUILR_MCP_DOWNSTREAM_URL", "http://127.0.0.1:9123/mcp")
    monkeypatch.setenv("QUILR_MCP_TIMEOUT_SECONDS", "2.5")
    settings = from_environment()
    assert settings.admin_token.get_secret_value() == ADMIN
    assert settings.viewer_token.get_secret_value() == VIEWER
    assert str(settings.downstream_url) == "http://127.0.0.1:9123/mcp"
    assert settings.timeout_seconds == 2.5
