"""Routes and matrix shaped like Routes API v2, from recorded responses."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from maps_mcp.errors import MapsError

STATION = "ChIJUU071tupbUcRyFdx20Nwgqg"


async def _route(service, origin, destination, **kw):
    args = {
        "intermediates": None,
        "travel_mode": "WALK",
        "departure_time": None,
        "arrival_time": None,
        "compute_alternative_routes": False,
        "include_steps": False,
        "include_polyline": False,
    }
    args.update(kw)
    return await service.compute_route(origin, destination, **args)


async def test_walk(service, transport):
    result = await _route(service, "Wien Hauptbahnhof", "ChIJpZ4LAn0HbUcRB3aToFL8ZUo")
    assert result["travelMode"] == "WALK" and result["condition"] == "ROUTE_EXISTS"
    route = result["routes"][0]
    assert route["distanceMeters"] == 774
    assert route["duration"] == "656s" and route["staticDuration"] == "656s"
    assert route["localizedValues"]["duration"] == {"text": "11 min"}
    assert route["description"] == "B221"
    assert route["durationRange"] is None and route["arrivalTime"] is None
    assert len(route["legs"]) == 1 and route["legs"][0]["endLocation"]["latLng"]["latitude"] == pytest.approx(48.1915)
    assert [w["displayName"] for w in result["waypoints"]] == ["Wien Hauptbahnhof", "Belvedere Palace"]
    assert result["waypoints"][0]["placeId"] == STATION
    link = parse_qs(urlsplit(result["googleMapsUri"]).query)
    assert link["travelmode"] == ["walking"] and link["origin_place_id"] == [STATION]
    # The place id was sent as its feature id.
    assert "0x476d077d020b9ea5" in transport.paths[0]
    assert result["note"] is None


async def test_drive_with_departure_time(service, transport):
    result = await _route(
        service,
        "Vienna International Airport",
        "Stephansplatz, Vienna",
        travel_mode="DRIVE",
        departure_time="2026-10-29T08:30",
        compute_alternative_routes=True,
    )
    assert "!19m2!2e2!3j1793262600" in transport.paths[0]  # 08:30 written as if UTC
    assert len(result["routes"]) == 2
    route = result["routes"][0]
    assert route["duration"] == "2100s"  # traffic predicted for 08:30
    assert route["staticDuration"] == "1638s"
    assert route["durationWithoutTraffic"] == "1344s"
    assert route["durationRange"] == {"min": "1560s", "max": "3000s", "text": "26–50 min"}
    assert route["arrivalTime"] == "2026-10-29T09:20:00+01:00"


async def test_arrival_time_gives_departure(service):
    result = await _route(
        service, "Vienna International Airport", "Stephansplatz", travel_mode="DRIVE", arrival_time="2026-10-29T09:00"
    )
    assert len(result["routes"]) == 1  # alternatives only on request
    assert result["routes"][0]["departureTime"] == "2026-10-29T08:10:00+01:00"


async def test_multi_stop_legs(service):
    result = await _route(
        service,
        "Schönbrunn Palace, Vienna",
        "Wiener Riesenrad, Prater, Vienna",
        intermediates=["Naschmarkt, Vienna"],
        travel_mode="DRIVE",
    )
    legs = result["routes"][0]["legs"]
    assert [(leg["distanceMeters"], leg["duration"]) for leg in legs] == [(4625, "717s"), (4008, "728s")]
    assert parse_qs(urlsplit(result["googleMapsUri"]).query)["waypoints"] == ["Naschmarkt, Wien, Austria"]


async def test_coordinates_resolve_to_areas_not_places(service):
    result = await _route(service, "48.2082,16.3738", "48.2035,16.3699")
    origin = result["waypoints"][0]
    assert origin["location"] == {"latitude": 48.2082, "longitude": 16.3738}
    assert origin["placeId"] is None  # an area id (GhIJ…) is not a place id


async def test_unknown_stop_names_it(service):
    with pytest.raises(MapsError) as err:
        await _route(service, "Qwxzv Plaza Nowhere 99999", "Albertina, Vienna")
    assert err.value.code == "waypoint_not_found"
    assert "stop 1 ('Qwxzv Plaza Nowhere 99999')" in err.value.payload()["next_step"]


async def test_no_route(service):
    result = await _route(service, "Vienna, Austria", "New York, USA", travel_mode="DRIVE")
    assert result["condition"] == "ROUTE_NOT_FOUND" and result["routes"] == [] and result["note"] is None


async def test_korea_has_no_walking_routes_note(service):
    result = await _route(service, "Haeundae Beach, Busan", "Gwangalli Beach, Busan")
    assert result["condition"] == "ROUTE_NOT_FOUND"
    assert "South Korea" in result["note"]


@pytest.mark.parametrize(
    ("kw", "fragment"),
    [
        ({"travel_mode": "TRANSIT"}, "travelMode"),
        ({"departure_time": "tomorrow"}, "departureTime"),
        ({"departure_time": "2026-10-29T08:30", "arrival_time": "2026-10-29T09:00"}, "not both"),
    ],
)
async def test_bad_arguments(service, kw, fragment):
    with pytest.raises(MapsError) as err:
        await _route(service, "A", "B", **kw)
    assert err.value.code == "invalid_argument" and fragment in err.value.payload()["next_step"]


async def test_bad_coordinates(service):
    with pytest.raises(MapsError) as err:
        await _route(service, "91.0,10.0", "B")
    assert err.value.code == "invalid_argument"


async def test_routes_are_cached(service, transport):
    await _route(service, "Wien Hauptbahnhof", "Belvedere, Vienna")
    await _route(service, "Wien Hauptbahnhof", "Belvedere, Vienna")
    assert len(transport.paths) == 1


async def test_turn_by_turn_steps_and_segments(service):
    result = await _route(
        service, "Vienna International Airport", "Stephansplatz", travel_mode="DRIVE", include_steps=True
    )
    leg = result["routes"][0]["legs"][0]
    first = leg["steps"][0]
    assert first["navigationInstruction"] == {"maneuver": None, "instructions": "Head southeast"}
    assert first["staticDuration"].endswith("s") and first["localizedValues"]["distance"]["text"]
    merge = next(s for s in leg["steps"] if s["navigationInstruction"]["maneuver"] == "MERGE")
    assert merge["navigationInstruction"]["instructions"] == "Merge onto A4" and merge["notices"] == ["Toll road"]
    assert leg["segments"][0] == {
        "instructions": "Get on A4",
        "distanceMeters": 2259,
        "staticDuration": "207s",
        "firstStepIndex": 0,
        "stepCount": 7,
    }
    assert sum(s["stepCount"] for s in leg["segments"]) == len(leg["steps"])
    assert result["routes"][0]["polyline"] is None


async def test_steps_are_off_by_default_and_polyline_on_request(service):
    plain = await _route(service, "Wien Hauptbahnhof", "Belvedere, Vienna")
    assert plain["routes"][0]["legs"][0]["steps"] is None
    lined = await _route(service, "Wien Hauptbahnhof", "Belvedere, Vienna", include_polyline=True)
    encoded = lined["routes"][0]["polyline"]["encodedPolyline"]
    assert isinstance(encoded, str) and len(encoded) > 50
    walk = await _route(service, "Wien Hauptbahnhof", "Belvedere, Vienna", include_steps=True)
    maneuvers = {s["navigationInstruction"]["maneuver"] for s in walk["routes"][0]["legs"][0]["steps"]}
    assert {"TURN_LEFT", "TURN_RIGHT"} <= maneuvers


async def test_matrix(service, transport):
    result = await service.compute_route_matrix(
        ["Wien Hauptbahnhof", "Qwxzv Plaza Nowhere 99999"],
        ["Wien Hauptbahnhof", "Belvedere, Vienna"],
        "WALK",
        None,
    )
    elements = {(e["originIndex"], e["destinationIndex"]): e for e in result["elements"]}
    assert elements[(0, 0)]["duration"] == "0s"  # same stop: no request
    assert elements[(0, 1)]["condition"] == "ROUTE_EXISTS" and elements[(0, 1)]["duration"] == "656s"
    assert elements[(1, 1)]["status"] == {"code": "NOT_FOUND", "message": "origin not found"}
    assert len(transport.paths) == 3


async def test_matrix_limits(service, state):
    with pytest.raises(MapsError) as err:
        await service.compute_route_matrix(["a"] * 5, ["b"] * 6, "DRIVE", None)
    assert err.value.code == "too_many_elements"
    state.take(state.remaining() - 1)
    with pytest.raises(MapsError) as err:
        await service.compute_route_matrix(["a", "b"], ["c", "d"], "DRIVE", None)
    assert err.value.code == "budget_exhausted"
