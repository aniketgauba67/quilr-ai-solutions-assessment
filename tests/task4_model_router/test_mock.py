"""The local provider fixture must be deterministic and must not echo prompts."""

import asyncio

import httpx
import pytest

from quilr_assessment.mocks.completion import COMPLETIONS, UPSTREAM_LEAK, create_app
from quilr_assessment.task4_model_router.tokens import count_tokens

pytestmark = pytest.mark.asyncio

REQUEST = {"prompt": "Explain retries", "max_output_tokens": 256, "scenario": "ok", "delay_ms": 0}


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://provider.invalid"
    )


@pytest.mark.parametrize("name", ["primary", "secondary"])
async def test_each_provider_returns_its_own_deterministic_completion(name: str) -> None:
    app = create_app()
    async with client(app) as http:
        first = await http.post(f"/{name}/completions", json=REQUEST)
        second = await http.post(f"/{name}/completions", json=REQUEST)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["text"] == COMPLETIONS[name]
    assert app.state.requests[name] == 2
    assert app.state.completed[name] == 2


async def test_usage_matches_the_shared_tokenizer_and_hides_the_prompt() -> None:
    prompt = "Contact jordan.lee@example.com about the refund"
    async with client(create_app()) as http:
        response = await http.post("/primary/completions", json={**REQUEST, "prompt": prompt})
    body = response.json()
    assert body["usage"]["prompt_tokens"] == count_tokens(prompt)
    assert body["usage"]["completion_tokens"] == count_tokens(COMPLETIONS["primary"])
    assert prompt not in response.text
    assert "jordan.lee@example.com" not in response.text


async def test_completion_tokens_respect_the_requested_output_budget() -> None:
    async with client(create_app()) as http:
        response = await http.post("/primary/completions", json={**REQUEST, "max_output_tokens": 3})
    assert response.json()["usage"]["completion_tokens"] == 3


@pytest.mark.parametrize(
    "scenario,status",
    [
        ("rate_limited", 429),
        ("server_error", 500),
        ("bad_request", 400),
        ("unauthorized", 401),
    ],
)
async def test_failure_scenarios_return_realistic_upstream_noise(
    scenario: str, status: int
) -> None:
    async with client(create_app()) as http:
        response = await http.post("/primary/completions", json={**REQUEST, "scenario": scenario})
    assert response.status_code == status
    # The fixture leaks on purpose so gateway tests can prove nothing is relayed.
    assert response.json()["error"] == UPSTREAM_LEAK


async def test_rate_limited_responses_carry_a_retry_after_header() -> None:
    async with client(create_app()) as http:
        response = await http.post(
            "/primary/completions", json={**REQUEST, "scenario": "rate_limited"}
        )
    assert response.headers["retry-after"] == "30"


async def test_malformed_scenario_returns_unparsable_json() -> None:
    async with client(create_app()) as http:
        response = await http.post(
            "/primary/completions", json={**REQUEST, "scenario": "malformed"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    with pytest.raises(ValueError):
        response.json()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": "hi"},
        {"prompt": "", "max_output_tokens": 5},
        {"prompt": "hi", "max_output_tokens": 0},
        {"prompt": "hi", "max_output_tokens": 5, "scenario": "unknown"},
        {"prompt": "hi", "max_output_tokens": 5, "extra": 1},
        {"prompt": "hi", "max_output_tokens": "5"},
    ],
)
async def test_invalid_provider_requests_are_rejected_without_echoing_them(payload: dict) -> None:
    app = create_app()
    async with client(app) as http:
        response = await http.post("/primary/completions", json=payload)
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid completion request"}
    assert app.state.requests["primary"] == 0


async def test_delay_is_honoured_and_counted() -> None:
    app = create_app()
    async with client(app) as http:
        response = await http.post("/secondary/completions", json={**REQUEST, "delay_ms": 10})
    assert response.status_code == 200
    assert app.state.requests == {"primary": 0, "secondary": 1}


async def test_cancelling_an_in_flight_request_is_recorded() -> None:
    app = create_app()
    async with client(app) as http:
        task = asyncio.create_task(
            http.post("/primary/completions", json={**REQUEST, "delay_ms": 5000})
        )
        async with asyncio.timeout(5):
            while app.state.requests["primary"] == 0:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert app.state.cancelled["primary"] == 1
    assert app.state.completed["primary"] == 0
