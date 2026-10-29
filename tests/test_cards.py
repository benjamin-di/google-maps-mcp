"""The cards: maps_show_route / maps_show_places results, their _meta and the view resource."""

from __future__ import annotations

import json

import mcp_types
from conftest import ALBERTINA, CAFE_CENTRAL
from fastmcp import Client

from maps_mcp import server


async def test_show_route_card(service):
    shown, view = await service.show_route(
        "Vienna International Airport",
        "Stephansplatz",
        None,
        "DRIVE",
        None,
        None,
        False,
        False,
        "Into town",
        "Leave by 9",
    )
    assert shown["kind"] == "route" and shown["title"] == "Into town" and shown["note"] == "Leave by 9"
    assert [s["label"] for s in shown["stops"]] == ["A", "B"]
    assert shown["googleMapsUri"].startswith("https://www.google.com/maps/dir/?api=1")
    # The line and the turns go to the view only.
    assert 2 <= len(view["path"]) <= 800 and len(view["path"][0]) == 2
    first = view["steps"][0][0]
    assert first["i"] == "Head southeast" and "m" in first and first["d"]
    assert any("Toll road" in s["n"] for s in view["steps"][0])
    assert "steps" not in json.dumps(shown)


async def test_show_places_cards(service):
    shown, view = await service.show_places(
        [{"id": ALBERTINA, "note": "Dürer's hare"}, {"id": CAFE_CENTRAL}],
        "Art then coffee",
    )
    albertina, cafe = shown["places"]
    assert (albertina["label"], cafe["label"]) == ("A", "B")
    assert albertina["displayName"] == "Albertina" and albertina["note"] == "Dürer's hare"
    assert albertina["phone"] == "+43 1 534830" and albertina["category"] == "Art museum"
    assert albertina["todayHours"] == "10 AM–9 PM" and albertina["openNow"] is True
    assert albertina["directionsUri"].endswith(f"destination_place_id={ALBERTINA}")
    assert cafe["displayName"] == "Café Central" and cafe["openNow"] is None and cafe["todayHours"] is None
    assert albertina["reservationUri"] is None  # Google lists no booking site for a museum
    assert view["photos"][ALBERTINA].startswith("https://lh3.googleusercontent.com/")
    assert "lh3.googleusercontent" not in json.dumps(shown)


async def test_cards_through_mcp(service, tiles):
    async with Client(server.create_server(service)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        for name in ("maps_show_route", "maps_show_places"):
            ui = (tools[name].meta or {}).get("ui") or {}
            assert ui.get("resourceUri") == server.VIEW_URI, tools[name].meta
        assert "ui" not in (tools["maps_compute_route"].meta or {})
        listed = {str(r.uri): r for r in await client.list_resources()}
        assert listed[server.VIEW_URI].mime_type == "text/html;profile=mcp-app"
        html = await client.read_resource(server.VIEW_URI)
        text = html[0].text if isinstance(html[0], mcp_types.TextResourceContents) else ""
        assert "ui/initialize" in text and "__VERSION__" not in text
        result = await client.call_tool("maps_show_places", {"items": [{"id": ALBERTINA}], "title": "Art"})
        assert result.structured_content["kind"] == "places"
        assert result.meta and server.VIEW_META in result.meta
        route = await client.call_tool(
            "maps_show_route",
            {"origin": "Vienna International Airport", "destination": "Stephansplatz", "title": "Drive"},
        )
        assert route.structured_content["kind"] == "route" and route.meta[server.VIEW_META]["path"]
