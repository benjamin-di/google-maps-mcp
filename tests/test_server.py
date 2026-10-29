"""The FastMCP surface: bearer auth over real HTTP, tool list, typed errors, status and canary."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from maps_mcp import server
from maps_mcp.auth import AuthError, bearer_auth_from_env
from maps_mcp.config import ConfigError, Settings

TOOLS = {
    "maps_search_places",
    "maps_get_place",
    "maps_compute_route",
    "maps_compute_route_matrix",
    "maps_static_map",
    "maps_plan_route",
    "maps_show_route",
    "maps_show_places",
    "maps_status",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}


def test_mcp_requires_a_bearer_and_health_is_open(service, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    app = server.create_server(service, auth=bearer_auth_from_env()).http_app(**server.HTTP_OPTIONS)
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        assert client.post("/mcp", headers=headers, json=INITIALIZE).status_code == 401
        good = {**headers, "Authorization": "Bearer test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        response = client.post("/mcp", headers=good, json=INITIALIZE)
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers  # stateless
        assert response.headers["content-type"].startswith("application/json")
        listed = client.post(
            "/mcp",
            headers={**good, "mcp-protocol-version": "2025-03-26", "Host": "maps-mcp:8000"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert {t["name"] for t in listed.json()["result"]["tools"]} == TOOLS
        assert client.get("/health").json() == {"status": "healthy"}


def test_serving_without_a_token_refuses(monkeypatch, capsys):
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    with pytest.raises(AuthError):
        bearer_auth_from_env()
    with pytest.raises(SystemExit) as exit_:
        server.main([])
    assert exit_.value.code == 1 and "MCP_BEARER_TOKEN" in capsys.readouterr().err


def test_several_tokens_are_accepted_and_short_ones_refused():
    one, two = "1" * 32, "2" * 40
    verifier = bearer_auth_from_env({"MCP_BEARER_TOKEN": f"{one}, {two}"})
    assert set(verifier.tokens) == {one, two}
    with pytest.raises(AuthError):
        bearer_auth_from_env({"MCP_BEARER_TOKEN": f"{one},short"})


def test_settings(tmp_path):
    settings = Settings.from_env({"XDG_STATE_HOME": str(tmp_path / "xdg")})
    assert settings.state_dir == tmp_path / "xdg" / "google-maps-mcp" and settings.state_dir.is_dir()
    assert settings.cookie_file == settings.state_dir / "google-cookies.json"
    assert settings.daily_request_budget == 1000 and settings.budget_tz == "UTC"
    assert settings.language == "en" and settings.canary_to.startswith("ChIJ")
    state = str(tmp_path / "state")
    custom = Settings.from_env(
        {
            "MAPS_STATE_DIR": state,
            "MAPS_BUDGET_TZ": "Europe/Vienna",
            "MAPS_CANARY_FROM": "48.2,16.37",
            "MAPS_LANGUAGE": "pt-BR",
        }
    )
    assert str(custom.state_dir) == state and custom.budget_tz == "Europe/Vienna"
    assert custom.canary_from == "48.2,16.37" and custom.language == "pt-BR"
    for bad in (
        {"MAPS_DAILY_REQUEST_BUDGET": "lots"},
        {"MAPS_MIN_INTERVAL_S": "0"},
        {"MAPS_BUDGET_TZ": "Mars/Olympus"},
        {"MAPS_CANARY_TO": "Albertina"},
        {"MAPS_LANGUAGE": "en&gl=x"},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({"MAPS_STATE_DIR": state, **bad})


def test_unwritable_state_dir_is_a_clear_startup_error(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    with pytest.raises(ConfigError, match="not writable"):
        Settings.from_env({"MAPS_STATE_DIR": str(locked / "state")})


def test_server_reports_its_own_version(service):
    from maps_mcp import __version__

    assert server.create_server(service).version == __version__


async def test_tools_end_to_end(service):
    async with Client(server.create_server(service)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == TOOLS
        assert all(t.annotations.read_only_hint for t in tools.values())
        route = await client.call_tool(
            "maps_compute_route", {"origin": "Wien Hauptbahnhof", "destination": "Belvedere, Vienna"}
        )
        assert route.structured_content["routes"][0]["duration"] == "656s"
        place = await client.call_tool("maps_get_place", {"id": "ChIJwVp-8psHbUcRuN43n5iK72Y"})
        assert place.structured_content["displayName"]["text"] == "Albertina"
        assert place.structured_content["reservationLinks"] == [] and place.structured_content["menuUri"] is None


async def test_errors_are_typed_and_never_echo_exceptions(service, transport):
    async with Client(server.create_server(service)) as client:
        with pytest.raises(ToolError) as err:
            await client.call_tool("maps_compute_route", {"origin": "Qwxzv Plaza Nowhere 99999", "destination": "x"})
        payload = json.loads(str(err.value))["error"]
        assert payload["code"] == "waypoint_not_found" and payload["retryable"] is False
        with pytest.raises(ToolError) as err:
            await client.call_tool("maps_compute_route", {"origin": "a", "destination": "b", "travelMode": "BOAT"})
        assert json.loads(str(err.value))["error"]["code"] == "invalid_argument"
        transport.responder = lambda path: RuntimeError("secret detail")
        with pytest.raises(ToolError) as err:
            await client.call_tool("maps_compute_route", {"origin": "c", "destination": "d"})
        assert "secret" not in str(err.value)
        assert json.loads(str(err.value))["error"]["code"] == "internal_error"


async def test_status_and_canary(service, transport, clock, tiles):
    status = await service.status(check=False)
    assert status["budget"]["requests_daily"] == 50 and "canary" not in status
    first = (await service.status(check=True))["canary"]
    assert first["ok"] is True and first["code"] == "ok" and first["cached"] is False
    assert tiles and tiles[0][:2] == ("m", 15)  # the canary also reads one map tile
    calls = len(transport.paths)
    again = await service.canary()
    assert again["cached"] is True and len(transport.paths) == calls
    clock[0] += 56 * 60
    assert (await service.canary())["cached"] is False


async def test_canary_reports_failures_without_raising(service, transport, clock):
    transport.responder = lambda path: [[["only one waypoint"]]]  # unknown layout
    result = await service.canary()
    assert result == {"ok": False, "code": "schema_changed", "checked_at": result["checked_at"], "cached": False}
    assert (await service.status(False))["last_error"] is None  # canary failures are not tool errors
