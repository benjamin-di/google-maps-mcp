"""The actual vendored clients must refuse unapproved destinations before network I/O."""

from __future__ import annotations

import json
import logging
import os
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from maps_mcp.errors import MapsError
from maps_mcp.google import GoogleMaps, HostNotAllowedError, default_client_factory
from maps_mcp.vendor.gmaps._auth.session import CookieSession
from maps_mcp.vendor.gmaps.exceptions import RequestHookError

REFUSED_URL = "https://www.example.com/refused?credential=synthetic-sensitive"


@pytest.fixture
def network(monkeypatch):
    """Intercept the final httpx transport; request hooks and redirects remain real."""
    sent = []
    responses = []

    async def handle(_transport, request):
        sent.append(request)
        assert responses, "unexpected network attempt"
        response = responses.pop(0)
        response.request = request
        return response

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return sent, responses


def _saved_consent(settings):
    settings.cookie_file.write_text(
        json.dumps({"cookies": {"NID": "synthetic", "AEC": "synthetic", "SOCS": "synthetic"}, "saved_at": time.time()})
    )


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("redirect", [False, True])
async def test_client_refuses_initial_and_redirected_targets(settings, network, monkeypatch, method, redirect):
    sent, responses = network
    backoff = AsyncMock()
    monkeypatch.setattr("maps_mcp.vendor.gmaps.transport._exponential_backoff", backoff)
    _saved_consent(settings)
    if redirect:
        responses.extend(
            [
                httpx.Response(307, headers={"Location": "https://maps.google.com/middle"}),
                httpx.Response(307, headers={"Location": REFUSED_URL}),
            ]
        )
    async with default_client_factory(settings) as client:
        with pytest.raises(RequestHookError, match="^Request hook failed$") as exc:
            await getattr(client._transport, method)("/start" if redirect else REFUSED_URL)

    assert isinstance(exc.value.__cause__, HostNotAllowedError)
    assert str(exc.value.__cause__) == "request to another host refused"
    assert [r.url.path for r in sent] == (["/start", "/middle"] if redirect else [])
    assert all(r.url.host in {"www.google.com", "maps.google.com"} for r in sent)
    backoff.assert_not_awaited()
    assert not responses


@pytest.mark.parametrize("host", ["www.google.com", "google.com", "maps.google.com", "consent.google.com"])
async def test_client_keeps_every_allowed_destination(settings, network, host):
    sent, responses = network
    _saved_consent(settings)
    responses.append(httpx.Response(200, json={"ok": True}))
    async with default_client_factory(settings) as client:
        result = await client._transport.get(f"https://{host}/allowed", response_type="json")

    assert result == {"ok": True}
    assert len(sent) == 1 and sent[0].url.host == host


@pytest.mark.parametrize("host", ["www.google.com.example.com", "example.com", "127.0.0.1"])
async def test_host_policy_is_exact(settings, network, host):
    sent, _responses = network
    _saved_consent(settings)
    async with default_client_factory(settings) as client:
        with pytest.raises(RequestHookError):
            await client._transport.get(f"https://{host}/refused")
    assert not sent


async def test_consent_client_also_refuses_an_initial_target(settings, network):
    sent, _responses = network
    client = default_client_factory(settings)
    session = client._cookie_session
    await session._init_client()
    try:
        with pytest.raises(RequestHookError) as exc:
            await session._client.get(REFUSED_URL)
        assert isinstance(exc.value.__cause__, HostNotAllowedError)
        assert not sent
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("stage", range(4))
async def test_consent_redirect_refusal_closes_setup_without_leaking_url(
    settings, state, network, monkeypatch, caplog, stage
):
    sent, responses = network
    monkeypatch.setattr("maps_mcp.vendor.gmaps._auth.session.asyncio.sleep", AsyncMock())
    original_init = CookieSession._init_client
    opened = []

    async def capture_client(session):
        await original_init(session)
        opened.append(session._client)

    monkeypatch.setattr(CookieSession, "_init_client", capture_client)
    responses.extend(httpx.Response(200, json={}) for _ in range(stage))
    responses.append(httpx.Response(302, headers={"Location": REFUSED_URL}))
    google = GoogleMaps(settings, state)
    with caplog.at_level(logging.INFO), pytest.raises(MapsError) as exc:
        await google.search("synthetic place", None, None, None, 1)

    assert exc.value.code == "upstream_error"
    assert exc.value.log_detail == "redirect to another host refused"
    assert REFUSED_URL not in caplog.text
    assert "synthetic-sensitive" not in str(exc.value.payload()) + caplog.text
    assert [r.url.path for r in sent] == ["/", "/ml", "/save", "/maps"][: stage + 1]
    assert all(r.url.host in {"www.google.com", "consent.google.com"} for r in sent)
    assert len(opened) == 1 and opened[0].is_closed
    assert google._client is None and not settings.cookie_file.exists()
    assert not responses


@pytest.mark.parametrize("redirect", [False, True])
async def test_wrapper_reports_policy_refusal_without_request_details(
    settings, state, network, monkeypatch, caplog, redirect
):
    sent, responses = network
    _saved_consent(settings)
    backoff = AsyncMock()
    monkeypatch.setattr("maps_mcp.vendor.gmaps.transport._exponential_backoff", backoff)
    if redirect:
        responses.append(httpx.Response(302, headers={"Location": REFUSED_URL}))
    google = GoogleMaps(settings, state)

    async def call(client):
        return await client._transport.get("/start" if redirect else REFUSED_URL)

    with caplog.at_level(logging.INFO), pytest.raises(MapsError) as exc:
        await google._run(call)
    assert exc.value.code == "upstream_error"
    assert REFUSED_URL not in str(exc.value.payload()) + caplog.text
    assert "synthetic-sensitive" not in str(exc.value.payload()) + caplog.text
    assert len(sent) == int(redirect)
    assert google._client is None
    backoff.assert_not_awaited()


async def test_unknown_hook_failure_does_not_expose_its_cause(google, caplog):
    failure = RequestHookError("Request hook failed")
    failure.__cause__ = ValueError(REFUSED_URL)
    with caplog.at_level(logging.ERROR):
        error = google._classify(failure)
    assert error.code == "internal_error"
    assert REFUSED_URL not in str(error.payload()) + caplog.text


def test_policy_failure_does_not_copy_cause_url_into_logs(google):
    failure = RequestHookError("Request hook failed")
    failure.__cause__ = HostNotAllowedError(REFUSED_URL)
    error = google._classify(failure)
    assert error.code == "upstream_error"
    assert error.log_detail == "redirect to another host refused"
    assert REFUSED_URL not in str(error.payload()) + str(error.log_detail)


async def test_saved_sign_in_cookies_never_reach_transport(settings, network):
    sent, responses = network
    _saved_consent(settings)
    data = json.loads(settings.cookie_file.read_text())
    data["cookies"]["SID"] = "synthetic-login"
    data["cookies"]["HSID"] = "synthetic-login"
    settings.cookie_file.write_text(json.dumps(data))
    responses.append(httpx.Response(200, json={}))
    async with default_client_factory(settings) as client:
        await client._transport.get("/allowed", response_type="json")
    assert len(sent) == 1
    assert "synthetic-login" not in sent[0].headers.get("cookie", "")
    assert "synthetic" in sent[0].headers["cookie"]


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require extra privileges")
async def test_fresh_consent_replaces_cache_symlink_privately(settings, network, monkeypatch):
    _sent, responses = network
    monkeypatch.setattr("maps_mcp.vendor.gmaps._auth.session.asyncio.sleep", AsyncMock())
    target = settings.state_dir / "unrelated.json"
    target.write_text('{"keep": "unrelated"}')
    settings.cookie_file.symlink_to(target)
    responses.extend(
        httpx.Response(200, json={}, headers=[("Set-Cookie", "NID=synthetic"), ("Set-Cookie", "AEC=synthetic")])
        for _ in range(4)
    )
    async with default_client_factory(settings) as client:
        assert client._cookie_session.is_valid
    assert not settings.cookie_file.is_symlink()
    assert settings.cookie_file.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == '{"keep": "unrelated"}'
    assert not list(settings.state_dir.glob(".cookies-*.tmp"))
