"""Multi-stop planning: order candidates, real-route choice, arrival times and opening hours."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import load

from maps_mcp import planner
from maps_mcp.errors import MapsError
from maps_mcp.vendor.gmaps.rpc.directions import Directions, DirectionsWaypoint, Route, RouteLeg
from maps_mcp.vendor.gmaps.rpc.parser import ParsedPlace, parse_opening_hours

# Origin HOME and stops in a line going east; the given order is deliberately the worst.
HOME = (48.20, 16.30)
EAST = {"near": (48.20, 16.32), "mid": (48.20, 16.34), "far": (48.20, 16.36)}


def test_candidate_orders_rank_by_straight_line_and_keep_the_given_order():
    stops = [EAST["far"], EAST["near"], EAST["mid"]]
    orders = planner.candidate_orders(HOME, stops, None, keep=2)
    assert orders[0] == (1, 2, 0)  # near, mid, far
    assert (0, 1, 2) in orders and len(orders) == 3
    assert planner.candidate_orders(HOME, [EAST["near"]], None) == [(0,)]


def test_fixed_end_and_round_trip_change_the_best_order():
    stops = [EAST["near"], EAST["far"]]
    assert planner.candidate_orders(HOME, stops, EAST["near"], keep=1)[0] == (1, 0)  # end near: far first
    rt = planner.candidate_orders(HOME, stops, HOME, keep=2)
    assert set(rt[:2]) == {(0, 1), (1, 0)}  # a loop is the same length either way


class FakeRoutes:
    """GoogleMaps.route stand-in: every point by name; duration = 600 s per 0.01 degree of travel."""

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.points = {"Home": HOME, **EAST}

    async def route(self, stops, mode, departure=None, arrival=None):
        self.calls.append(list(stops))
        waypoints, coords = [], []
        for stop in stops:
            if isinstance(stop, tuple):
                name, point = None, stop
            else:
                name = next((k for k, v in self.points.items() if f"0x{k}" == stop), stop)
                point = self.points.get(name)
            coords.append(point)
            hex_id = f"0x{name}" if name else ""
            waypoints.append(
                DirectionsWaypoint(
                    query=str(stop), name=name or "", hex_id=hex_id, latitude=point[0], longitude=point[1]
                )
            )
        legs = []
        for a, b in zip(coords, coords[1:], strict=False):
            seconds = round(abs(b[1] - a[1]) * 60_000)
            legs.append(RouteLeg(distance_meters=seconds * 2, duration_seconds=seconds, duration_text=f"{seconds}s"))
        total = sum(leg.duration_seconds for leg in legs)
        route = Route(
            summary="Test Rd",
            distance_meters=total * 2,
            duration_seconds=total,
            static_duration_seconds=total,
            legs=legs,
        )
        return Directions(mode=mode, status="OK", waypoints=waypoints, routes=[route])


@pytest.fixture
def fake(service):
    routes = FakeRoutes()
    service.google.route = routes.route
    return routes


async def _plan(service, stops, **kw):
    args = {
        "destination": None,
        "round_trip": False,
        "travel_mode": "DRIVE",
        "departure_time": "2026-10-26T09:00",
        "dwell_minutes": None,
        "optimize_order": True,
    }
    args.update(kw)
    plan, route, waypoints = await service.plan_route("Home", stops, **args)
    return plan


async def test_optimize_picks_the_fastest_real_order_and_reports_the_saving(service, fake):
    plan = await _plan(service, ["far", "near", "mid"])
    assert plan["order"] == [1, 2, 0]
    assert [s["displayName"] for s in plan["stops"]] == ["Home", "near", "mid", "far"]
    assert [s["label"] for s in plan["stops"]] == ["A", "B", "C", "D"]
    assert plan["duration"] == "3600s"  # 0.06 degrees east
    assert plan["savedVsGivenOrder"] == "3600s"  # given: far, near, mid = 0.06 + 0.04 + 0.02 degrees = 7200 s
    assert plan["optimized"] is True and plan["ordersCompared"] == 5
    # Candidates are routed by what Google resolved (feature ids), not re-geocoded text.
    assert fake.calls[1][0] == "0xHome"


async def test_given_order_is_kept_without_optimizing(service, fake):
    plan = await _plan(service, ["far", "near"], optimize_order=False)
    assert plan["order"] == [0, 1] and plan["optimized"] is False and plan["savedVsGivenOrder"] is None
    assert len(fake.calls) == 1


async def test_round_trip_ends_at_the_origin_as_pin_a(service, fake):
    plan = await _plan(service, ["near", "far"], round_trip=True)
    assert plan["stops"][-1]["label"] == "A" and plan["stops"][-1]["role"] == "origin"
    assert plan["duration"] == "7200s"


async def test_arrival_times_dwell_and_opening_hours(service, fake, monkeypatch):
    sample = load("hours_samples.json")["SPAR Gourmet"]
    spar = ParsedPlace(name="SPAR", timezone="Europe/Vienna", opening_hours=parse_opening_hours(sample["hours_203"]))

    async def record(place_id):
        return spar

    monkeypatch.setattr(service, "_place_record", record)
    # Make every waypoint look like a place so its hours are read.
    original = fake.route

    async def with_ids(stops, mode, departure=None, arrival=None):
        result = await original(stops, mode, departure, arrival)
        for w in result.waypoints:
            w.place_id = "ChIJwVp-8psHbUcRuN43n5iK72Y"
        return result

    service.google.route = with_ids
    # Monday 26 Oct (Austrian National Day: SPAR closed), and Tuesday 27 Oct at 19:30 (closes 20:00).
    monday = await _plan(service, ["near"], departure_time="2026-10-26T09:00", dwell_minutes=[30])
    stop = monday["stops"][1]
    assert stop["arrivalTime"] == "2026-10-26T09:20:00+01:00"
    assert stop["departureTime"] == "2026-10-26T09:50:00+01:00"
    assert stop["openingStatus"]["state"] == "closed"
    assert monday["warnings"] == ["B near: " + stop["openingStatus"]["text"]]
    assert monday["durationWithDwell"] == "3000s"
    evening = await _plan(service, ["near"], departure_time="2026-10-27T19:00", dwell_minutes=[45])
    assert evening["stops"][1]["openingStatus"] == {
        "state": "closes_during_visit",
        "text": "Closes 20:00",
        "hours": "7:30\u202fAM–8\u202fPM",
    }
    morning = await _plan(service, ["near"], departure_time="2026-10-27T09:00")
    assert morning["stops"][1]["openingStatus"]["state"] == "open"
    assert morning["stops"][1]["openingStatus"]["text"] == "Open until 20:00"


@pytest.mark.parametrize(
    ("stops", "kw", "fragment"),
    [
        (["a"] * 9, {}, "at most 8"),
        ([], {}, "stops, a destination"),
        (["near"], {"destination": "far", "round_trip": True}, "not both"),
        (["near"], {"dwell_minutes": [10, 20]}, "dwellMinutes"),
        (["near"], {"travel_mode": "TRANSIT"}, "travelMode"),
    ],
)
async def test_plan_arguments(service, fake, stops, kw, fragment):
    with pytest.raises(MapsError) as err:
        await _plan(service, stops, **kw)
    assert err.value.code == "invalid_argument" and fragment in err.value.payload()["next_step"]


async def test_no_route_is_its_own_error(service, fake):
    async def nothing(stops, mode, departure=None, arrival=None):
        result = await FakeRoutes().route(stops, mode)
        result.routes = []
        result.status = "ZERO_RESULTS"
        return result

    service.google.route = nothing
    with pytest.raises(MapsError) as err:
        await _plan(service, ["near"])
    assert err.value.code == "route_not_found"


def test_visit_status_without_hours_or_zone_is_unknown():
    from maps_mcp import shapes

    now = dt.datetime(2026, 10, 21, 12, tzinfo=dt.UTC)
    assert shapes.visit_status(ParsedPlace(name="x"), now, now)["state"] == "unknown"
