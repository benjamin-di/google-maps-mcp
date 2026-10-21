"""Shared fixtures. Tests never reach Google: the vendored library's real Search/Places/Directions
APIs run against a fake transport that answers with responses recorded from google.com/maps
(tests/fixtures, recorded and scrubbed by scripts/capture-fixtures: Vienna, plus Busan)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from maps_mcp.config import Settings
from maps_mcp.google import GoogleMaps
from maps_mcp.service import MapsService
from maps_mcp.state import StateStore
from maps_mcp.vendor.gmaps._directions import DirectionsAPI
from maps_mcp.vendor.gmaps._places import PlacesAPI
from maps_mcp.vendor.gmaps._search import SearchAPI

FIXTURES = Path(__file__).parent / "fixtures"
# 2026-10-21 12:00 in Vienna (Wednesday, 10:00 UTC), the day the fixtures were recorded.
FIXTURE_NOW = 1792576800.0
ALBERTINA = "ChIJwVp-8psHbUcRuN43n5iK72Y"
CAFE_CENTRAL = "ChIJLzsM95cHbUcRSS5C-z-H-ls"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """Answers each GET with ``responder(path)``; records every path."""

    def __init__(self, responder: Callable[[str], Any]) -> None:
        self.responder = responder
        self.paths: list[str] = []

    async def get(self, path: str, **kwargs: Any) -> Any:
        self.paths.append(path)
        result = self.responder(path)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClient:
    """Stands in for GMapsClient: the real feature APIs over a FakeTransport."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.search = SearchAPI(transport, "en", validate=False)  # type: ignore[arg-type]
        self.places = PlacesAPI(transport, "en")  # type: ignore[arg-type]
        self.directions = DirectionsAPI(transport, "en")  # type: ignore[arg-type]
        self.opened = 0
        self.closed = 0

    async def __aenter__(self) -> FakeClient:
        self.opened += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed += 1


def default_responder(path: str) -> Any:
    """Route a request path to the matching recorded response."""
    if path.startswith("/maps/preview/directions"):
        if "Qwxzv" in path:
            return load("unknown.json")
        if "New%20York" in path:
            return load("noroute_drive.json")
        if "Haeundae" in path:
            return load("korea_walk.json")
        if "!19m3!1e1" in path:
            return load("drive_arrive.json")
        if "!19m2!2e2" in path or "Airport" in path:
            return load("drive_depart.json")
        if "Naschmarkt" in path:
            return load("drive_3_stop.json")
        if "!3d48.2082" in path:
            return load("walk_coords.json")
        return load("walk_text.json")
    if path.startswith("/maps/preview/place"):
        if "0x476d079bf27e5ac1" in path:
            return load("details_albertina.json")
        if "0x476d0797f70c3b2f" in path:
            return load("details_cafe.json")
        return [None] * 3  # Google's answer for an unknown feature: no place data
    if path.startswith("/search"):
        if "Sperl" in path:
            return load("search_single.json")
        return load("search_museums.json")
    raise AssertionError(f"unexpected request {path}")


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(default_responder)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, daily_request_budget=50, min_interval_s=0.2)


@pytest.fixture
def clock() -> list[float]:
    return [FIXTURE_NOW]


@pytest.fixture
def state(settings: Settings, clock: list[float]) -> StateStore:
    return StateStore(settings.state_dir, request_budget=settings.daily_request_budget, clock=lambda: clock[0])


@pytest.fixture
def clients(transport: FakeTransport) -> list[FakeClient]:
    return []


@pytest.fixture
def google(settings: Settings, state: StateStore, transport: FakeTransport, clients: list[FakeClient], clock):
    settings.cookie_file.write_text("{}")  # consent cookies "saved": no warm-up charge

    def factory(_settings: Settings) -> FakeClient:
        client = FakeClient(transport)
        clients.append(client)
        return client

    return GoogleMaps(settings, state, client_factory=factory, clock=lambda: clock[0])


def png_tile(color: tuple[int, int, int] = (240, 238, 230)) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def tiles(google: GoogleMaps, monkeypatch) -> list[tuple[str, int, int, int]]:
    """Replace tile downloads with a plain tile; records (layer, zoom, x, y)."""
    calls: list[tuple[str, int, int, int]] = []
    tile = png_tile()

    async def fake_tile(layer: str, zoom: int, x: int, y: int) -> bytes:
        calls.append((layer, zoom, x, y))
        return tile

    monkeypatch.setattr(google, "tile", fake_tile)
    return calls


@pytest.fixture
def service(google: GoogleMaps, state: StateStore, clock) -> MapsService:
    return MapsService(google, state, clock=lambda: clock[0])
