"""maps_mcp.places: the library entry point over the same service, budget, breaker and errors."""

from __future__ import annotations

import pytest
from conftest import ALBERTINA

from maps_mcp.config import ConfigError
from maps_mcp.places import ERROR_CODES, RETRYABLE_CODES, MapsError, PlacesClient


@pytest.fixture
def client(tmp_path, service):
    places = PlacesClient(state_dir=tmp_path / "lib", daily_request_budget=10, budget_tz="Europe/Vienna")
    places._service = service  # the recorded responses, not Google
    return places


def test_constructor_configures_without_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPS_DAILY_REQUEST_BUDGET", "5")
    monkeypatch.setenv("MAPS_STATE_DIR", str(tmp_path / "env"))
    places = PlacesClient(state_dir=tmp_path / "lib", daily_request_budget=200, budget_tz="Europe/Vienna")
    assert places.settings.state_dir == tmp_path / "lib" and (tmp_path / "lib").is_dir()
    assert places.settings.daily_request_budget == 200 and places.settings.budget_tz == "Europe/Vienna"
    status = places.status()
    assert status["budget"]["requests_daily"] == 200 and status["budget"]["time_zone"] == "Europe/Vienna"


@pytest.mark.parametrize(
    "bad", [{"daily_request_budget": -1}, {"budget_tz": "Mars/Olympus"}, {"language": "not a tag"}]
)
def test_bad_settings_are_config_errors(tmp_path, bad):
    with pytest.raises(ConfigError):
        PlacesClient(state_dir=tmp_path, **bad)


async def test_same_shapes_as_the_tools(client, service):
    found = await client.search_places("museum", latitude=48.2049, longitude=16.3684, radius_meters=2000, page_size=2)
    assert found == await service.search_places("museum", 48.2049, 16.3684, 2000, 2)
    place = await client.get_place(ALBERTINA)
    assert place == await service.get_place(ALBERTINA)
    assert {"reservationLinks", "menuUri", "currentOpeningHours"} <= set(place)


async def test_errors_are_typed(client, service, transport, state):
    with pytest.raises(MapsError) as err:
        await client.get_place("Albertina")
    assert err.value.code == "invalid_argument" and not err.value.retryable
    with pytest.raises(MapsError) as err:
        await client.search_places("cafe", page_size=50)
    assert err.value.code == "invalid_argument" and "page_size" in err.value.next_step
    transport.responder = lambda path: RuntimeError("secret detail")
    with pytest.raises(MapsError) as err:
        await client.get_place("ChIJAAAAAAAABDQRDFz86-Qpbto")
    assert err.value.code in ERROR_CODES and err.value.code == "internal_error"
    assert err.value.retryable and "internal_error" in RETRYABLE_CODES
    assert state.describe()["last_error"]["code"] == "internal_error"


async def test_budget_and_breaker_apply(client, state):
    state.trip_breaker()
    with pytest.raises(MapsError) as err:
        await client.get_place(ALBERTINA)
    assert err.value.code == "blocked"


async def test_close(client):
    async with client as places:
        await places.get_place(ALBERTINA)
