"""GoogleMaps: budget, breaker, single slot, client recycling and error classification."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from maps_mcp.errors import MapsError
from maps_mcp.google import CLIENT_MAX_AGE_S, WARMUP_REQUESTS
from maps_mcp.vendor.gmaps.exceptions import AuthError, NetworkError, ParseError, RateLimitError
from maps_mcp.vendor.gmaps.exceptions import TimeoutError as GMapsTimeout


def _status_error(code: int) -> NetworkError:
    request = httpx.Request("GET", "https://www.google.com/x")
    response = httpx.Response(code, request=request)
    return NetworkError(f"HTTP {code}", original_error=httpx.HTTPStatusError("x", request=request, response=response))


async def test_each_call_spends_budget(google, state):
    before = state.remaining()
    await google.route(["A", "B"], "walk")
    assert state.remaining() == before - 1


async def test_budget_exhausted(google, state):
    state.take(state.remaining())
    with pytest.raises(MapsError) as err:
        await google.route(["A", "B"], "walk")
    assert err.value.code == "budget_exhausted"


async def test_first_open_without_cookies_charges_the_warm_up(google, settings, state):
    settings.cookie_file.unlink()
    before = state.remaining()
    await google.route(["A", "B"], "walk")
    assert state.remaining() == before - 1 - WARMUP_REQUESTS


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (RateLimitError("blocked"), "blocked"),
        (AuthError("login"), "blocked"),
        (ParseError("layout"), "schema_changed"),
        (_status_error(400), "schema_changed"),
        (_status_error(503), "upstream_error"),
        (GMapsTimeout("slow"), "upstream_error"),
        (NetworkError("reset"), "upstream_error"),
        (KeyError("x"), "internal_error"),
        (ValueError("secret detail from a parser"), "internal_error"),
        (__import__("json").JSONDecodeError("bad", "doc", 0), "schema_changed"),
    ],
)
async def test_classification(google, transport, clients, exc, code):
    transport.responder = lambda path: exc
    with pytest.raises(MapsError) as err:
        await google.route(["A", "B"], "walk")
    assert err.value.code == code
    assert "secret" not in str(err.value.payload())  # never exception text
    assert clients[0].closed == 1  # a failed client is dropped


async def test_block_opens_the_breaker(google, transport, state):
    transport.responder = lambda path: RateLimitError("sorry")
    with pytest.raises(MapsError):
        await google.route(["A", "B"], "walk")
    calls = len(transport.paths)
    with pytest.raises(MapsError) as err:
        await google.route(["A", "B"], "walk")
    assert err.value.code == "blocked" and len(transport.paths) == calls  # no request while open
    assert state.describe()["breaker"]["open"] is True


async def test_client_is_reused_then_recycled(google, clients, clock):
    await google.route(["A", "B"], "walk")
    await google.route(["A", "B"], "walk")
    assert len(clients) == 1
    clock[0] += CLIENT_MAX_AGE_S + 1
    await google.route(["A", "B"], "walk")
    assert len(clients) == 2 and clients[0].closed == 1


async def test_one_request_at_a_time(google, transport, monkeypatch):
    import maps_mcp.google as module

    monkeypatch.setattr(module, "SLOT_WAIT_S", 0.05)
    gate = asyncio.Event()
    original = transport.get

    async def slow_get(path, **kwargs):
        await gate.wait()
        return await original(path, **kwargs)

    transport.get = slow_get  # type: ignore[method-assign]
    first = asyncio.create_task(google.route(["A", "B"], "walk"))
    await asyncio.sleep(0.01)
    with pytest.raises(MapsError) as err:
        await google.route(["A", "B"], "walk")
    assert err.value.code == "busy"
    gate.set()
    assert (await first).status == "OK"
