"""maps_static_map: viewport fitting, tile stitching, route line and pins; tile fetching rules."""

from __future__ import annotations

import io

import httpx
import pytest
from conftest import png_tile
from PIL import Image

from maps_mcp import staticmap
from maps_mcp.errors import MapsError


def test_mercator_round_trip_and_known_tile():
    x, y = staticmap.world_px(48.2049, 16.3684, 15)
    lat, lng = staticmap.latlng(x, y, 15)
    assert (lat, lng) == pytest.approx((48.2049, 16.3684), abs=1e-9)
    # Independent check: the standard slippy-map tile formula.
    import math

    n = 2**15
    tile_x = int((16.3684 + 180) / 360 * n)
    tile_y = int((1 - math.asinh(math.tan(math.radians(48.2049))) / math.pi) / 2 * n)
    assert (int(x // 256), int(y // 256)) == (tile_x, tile_y)


def _span(pts, zoom):
    xs, ys = zip(*(staticmap.world_px(lat, lng, zoom) for lat, lng in pts), strict=True)
    return max(xs) - min(xs), max(ys) - min(ys)


def test_fit_picks_the_highest_zoom_that_shows_everything():
    pts = [(48.117948, 16.566258), (48.208733, 16.373676)]
    (lat, lng), zoom = staticmap.fit(pts, 640, 480)
    inner_w, inner_h = 640 - 2 * staticmap.PADDING, 480 - 2 * staticmap.PADDING
    w, h = _span(pts, zoom)
    assert w <= inner_w and h <= inner_h  # fits at this zoom
    w, h = _span(pts, zoom + 1)
    assert w > inner_w or h > inner_h  # but not one level closer
    assert lat == pytest.approx(48.163, abs=0.01) and lng == pytest.approx(16.470, abs=0.01)
    assert staticmap.fit([(48.2, 16.37)], 640, 480) == ((48.2, 16.37), 16)


def test_tiles_needed_covers_the_image():
    tiles = staticmap.tiles_needed((48.2049, 16.3684), 15, 640, 480)
    assert 6 <= len(tiles) <= 12


async def test_route_map(service, tiles):
    legend, jpeg = await service.static_map(
        None,
        None,
        "640x480",
        "roadmap",
        None,
        {"origin": "Vienna International Airport", "destination": "Stephansplatz"},
    )
    image = Image.open(io.BytesIO(jpeg))
    assert image.size == (640, 480) and image.format == "JPEG"
    assert legend["missingTiles"] == 0
    # Every point of the route lands inside the image.
    from conftest import load

    from maps_mcp.vendor.gmaps.rpc.directions import parse_directions_response

    path = parse_directions_response(load("drive_depart.json")).routes[0].path
    cx, cy = staticmap.world_px(legend["center"]["latitude"], legend["center"]["longitude"], legend["zoom"])
    for lat, lng in path:
        x, y = staticmap.world_px(lat, lng, legend["zoom"])
        assert 0 <= x - cx + 320 <= 640 and 0 <= y - cy + 240 <= 480
    assert [m["label"] for m in legend["markers"]] == ["A", "B"]
    assert legend["markers"][1]["displayName"] == "Stephansplatz"
    assert legend["route"]["condition"] == "ROUTE_EXISTS" and legend["route"]["duration"].endswith("s")
    assert legend["route"]["googleMapsUri"].startswith("https://www.google.com/maps/dir/?api=1")
    assert {t[0] for t in tiles} == {"m"} and {t[1] for t in tiles} == {legend["zoom"]}
    # The route is drawn in blue: some pixels are clearly blue on a beige base map.
    raw = image.convert("RGB").tobytes()
    pixels = [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]
    assert sum(1 for r, g, b in pixels if b > 180 and r < 90) > 200
    # Pins are red.
    assert sum(1 for r, g, b in pixels if r > 200 and g < 100 and b < 100) > 50


async def test_area_map_with_center_zoom_and_satellite(service, tiles):
    legend, jpeg = await service.static_map("48.2049,16.3684", 16, "400x300", "satellite", ["museum"], None)
    assert legend["zoom"] == 16 and legend["center"] == {"latitude": 48.2049, "longitude": 16.3684}
    assert (
        legend["markers"][0]["label"] == "A" and legend["markers"][0]["displayName"] == "Kunsthistorisches Museum Wien"
    )
    assert {t[0] for t in tiles} == {"s"}
    assert Image.open(io.BytesIO(jpeg)).size == (400, 300)


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ((None, None, "640x480", "roadmap", None, None), "center, markers or a route"),
        (("48.2,16.37", None, "2000x480", "roadmap", None, None), "between 128 and 1024"),
        (("48.2,16.37", None, "big", "roadmap", None, None), "WIDTHxHEIGHT"),
        (("48.2,16.37", None, "640x480", "watercolor", None, None), "maptype"),
    ],
)
async def test_static_map_arguments(service, tiles, args, fragment):
    with pytest.raises(MapsError) as err:
        await service.static_map(*args)
    assert err.value.code == "invalid_argument" and fragment in err.value.payload()["next_step"]


async def test_no_tiles_at_all_is_an_upstream_error(service, google, monkeypatch):
    async def none(*args):
        return None

    monkeypatch.setattr(google, "tile", none)
    with pytest.raises(MapsError) as err:
        await service.static_map("48.2,16.37", 15, "256x256", "roadmap", None, None)
    assert err.value.code == "upstream_error"


# ── GoogleMaps.tile against a mock HTTP transport ──


def _mock(google, handler):
    google._tile_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_tile_fetch_caches_and_spends_the_tile_budget(google, state):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png_tile())

    _mock(google, handler)
    before = state.describe()["budget"]["tiles_left"]
    first = await google.tile("m", 15, 26774, 14289)
    again = await google.tile("m", 15, 26774, 14289)
    assert first == again and len(seen) == 1
    assert seen[0].host == "www.google.com" and seen[0].path == "/maps/vt"
    assert dict(seen[0].params) == {"lyrs": "m", "hl": "en", "x": "26774", "y": "14289", "z": "15"}
    assert state.describe()["budget"]["tiles_left"] == before - 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429),
        httpx.Response(302, headers={"location": "https://www.google.com/sorry/index?continue=x"}),
    ],
)
async def test_tile_pushback_opens_the_breaker(google, state, response):
    _mock(google, lambda request: response)
    with pytest.raises(MapsError) as err:
        await google.tile("m", 15, 1, 1)
    assert err.value.code == "blocked" and state.describe()["breaker"]["open"] is True


async def test_tile_without_an_image_is_a_gap(google):
    _mock(google, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"))
    assert await google.tile("m", 15, 1, 1) is None


async def test_tool_returns_legend_and_image(service, tiles):
    from fastmcp import Client

    from maps_mcp import server

    async with Client(server.create_server(service)) as client:
        result = await client.call_tool(
            "maps_static_map",
            {"route": {"origin": "Vienna International Airport", "destination": "Stephansplatz"}, "size": "320x240"},
        )
    kinds = [c.type for c in result.content]
    assert kinds == ["text", "image"] and result.content[1].mime_type == "image/jpeg"
    assert result.structured_content["markers"][0]["label"] == "A"
