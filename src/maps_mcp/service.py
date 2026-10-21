"""Tool implementations: argument checks, caching and shaping around ``GoogleMaps``."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from . import __version__, planner, shapes, staticmap
from .errors import MapsError, invalid
from .google import GoogleMaps
from .state import StateStore, iso
from .vendor.gmaps.rpc.directions import Directions, DirectionsWaypoint, Route
from .vendor.gmaps.rpc.ids import hex_id_to_place_id, place_id_to_hex_id
from .vendor.gmaps.rpc.parser import ParsedPlace

log = logging.getLogger(__name__)

COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
MAX_MATRIX_ELEMENTS = 25
MAX_PLAN_STOPS = 8
CANARY_TTL_S = 55 * 60
CANARY_RETRY_S = 5 * 60
TRANSIENT = frozenset({"busy", "upstream_error", "internal_error"})
# Google has no walking/driving routes in South Korea (it resolves the places, then finds nothing).
KOREA = (33.0, 38.9, 124.5, 131.0)


class TTLCache:
    """Small in-memory LRU with per-entry expiry; values are parsed results, shaped per call."""

    def __init__(self, max_entries: int = 500, clock: Callable[[], float] = time.monotonic) -> None:
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._max = max_entries
        self._clock = clock

    def get(self, key: Any) -> Any:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self._clock():
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: Any, value: Any, ttl_s: float) -> None:
        self._items[key] = (self._clock() + ttl_s, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


def parse_stop(value: str) -> str | tuple[float, float]:
    text = (value or "").strip()
    if not text:
        raise invalid("a stop is empty")
    match = COORDS.match(text)
    if match:
        lat, lng = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise invalid(f"coordinates out of range: {text}")
        return (lat, lng)
    return text


def parse_local_time(value: str | None, name: str) -> dt.datetime | None:
    if value is None or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise invalid(f"{name} must be local time like 2026-10-22T08:30") from None
    # Maps wants the stop's wall-clock time; an offset, if given, is dropped, not converted.
    return moment.replace(tzinfo=None)


def simplify(points: list[tuple[float, float]], limit: int) -> list[list[float]]:
    """Every k-th point (ends kept) so a card's line stays under ``limit`` points."""
    if len(points) <= limit:
        return [[round(lat, 6), round(lng, 6)] for lat, lng in points]
    step = -(-len(points) // limit)
    kept = points[::step]
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return [[round(lat, 6), round(lng, 6)] for lat, lng in kept]


def _route_ttl(mode: str, timed: bool) -> float:
    if mode == "drive":
        return 3600.0 if timed else 600.0  # live traffic goes stale fast
    return 12 * 3600.0


def _in_korea(result: Directions) -> bool:
    south, north, west, east = KOREA
    return any(
        w.resolved and south <= (w.latitude or 0) <= north and west <= (w.longitude or 0) <= east
        for w in result.waypoints
    )


class MapsService:
    def __init__(
        self,
        google: GoogleMaps,
        state: StateStore,
        *,
        language: str = "en",
        clock: Callable[[], float] = time.time,
        cache: TTLCache | None = None,
        canary: tuple[str, str] = ("ChIJUU071tupbUcRyFdx20Nwgqg", "ChIJwVp-8psHbUcRuN43n5iK72Y"),
    ) -> None:
        self.google = google
        self.state = state
        self.language = language
        self._clock = clock
        self._cache = cache or TTLCache()
        self._canary: dict[str, Any] | None = None
        self._canary_places = canary

    def _now(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self._clock(), dt.UTC)

    # -- places ---------------------------------------------------------------------------

    async def search_places(
        self,
        text_query: str,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
        page_size: int = 5,
    ) -> dict[str, Any]:
        query = text_query.strip()
        if not query:
            raise invalid("textQuery is empty")
        if (latitude is None) != (longitude is None):
            raise invalid("give both latitude and longitude for the location bias, or neither")
        key = ("search", query.lower(), latitude, longitude, radius_meters, page_size)
        places: list[ParsedPlace] | None = self._cache.get(key)
        if places is None:
            places = await self.google.search(query, latitude, longitude, radius_meters, page_size)
            self._cache.put(key, places, 6 * 3600)
        now = self._now()
        return {"places": [shapes.place(p, now, self.language) for p in places[:page_size]]}

    async def get_place(self, place_id: str) -> dict[str, Any]:
        text = place_id.strip()
        if place_id_to_hex_id(text) is None:
            hex_place = hex_id_to_place_id(text)
            if hex_place is None:
                raise invalid("id must be a place ID starting with ChIJ (from maps_search_places) or 0x…:0x…")
            text = hex_place
        return shapes.place(await self._place_record(text), self._now(), self.language)

    async def _place_record(self, place_id: str) -> ParsedPlace:
        cached: ParsedPlace | None = self._cache.get(("details", place_id))
        if cached is None:
            cached = await self.google.place(place_id)
            if cached is None or not cached.name:
                raise MapsError("place_not_found")
            self._cache.put(("details", place_id), cached, 6 * 3600)
        return cached

    # -- routes ---------------------------------------------------------------------------

    async def _directions(
        self,
        stops: list[str | tuple[float, float]],
        mode: str,
        departure: dt.datetime | None,
        arrival: dt.datetime | None,
    ) -> Directions:
        key = ("route", tuple(stops), mode, departure, arrival)
        result: Directions | None = self._cache.get(key)
        if result is None:
            result = await self.google.route(stops, mode, departure, arrival)
            if result.status != "NOT_FOUND":
                self._cache.put(key, result, _route_ttl(mode, departure is not None or arrival is not None))
        return result

    async def compute_route(
        self,
        origin: str,
        destination: str,
        intermediates: list[str] | None,
        travel_mode: str,
        departure_time: str | None,
        arrival_time: str | None,
        compute_alternative_routes: bool,
        include_steps: bool = False,
        include_polyline: bool = False,
    ) -> dict[str, Any]:
        mode = shapes.TRAVEL_MODES.get(travel_mode)
        if mode is None:
            raise invalid("travelMode must be DRIVE, WALK or BICYCLE")
        departure = parse_local_time(departure_time, "departureTime")
        arrival = parse_local_time(arrival_time, "arrivalTime")
        if departure and arrival:
            raise invalid("give departureTime or arrivalTime, not both")
        stops = [parse_stop(origin), *(parse_stop(s) for s in intermediates or []), parse_stop(destination)]
        result = await self._directions(stops, mode, departure, arrival)
        if result.status == "NOT_FOUND":
            names = ", ".join(f"stop {i + 1} ('{result.waypoints[i].query[:80]}')" for i in result.unresolved)
            raise MapsError("waypoint_not_found", f"Not found: {names}")
        shaped = shapes.directions(
            result,
            mode,
            alternatives=compute_alternative_routes,
            include_steps=include_steps,
            include_polyline=include_polyline,
        )
        shaped["condition"] = "ROUTE_EXISTS" if result.routes else "ROUTE_NOT_FOUND"
        shaped["note"] = None
        if not result.routes and _in_korea(result):
            shaped["note"] = "Google Maps has no walking or driving routes in South Korea; use Naver Map or KakaoMap."
        return shaped

    async def compute_route_matrix(
        self,
        origins: list[str],
        destinations: list[str],
        travel_mode: str,
        departure_time: str | None,
    ) -> dict[str, Any]:
        mode = shapes.TRAVEL_MODES.get(travel_mode)
        if mode is None:
            raise invalid("travelMode must be DRIVE, WALK or BICYCLE")
        if not origins or not destinations:
            raise invalid("give at least one origin and one destination")
        if len(origins) * len(destinations) > MAX_MATRIX_ELEMENTS:
            raise MapsError(
                "too_many_elements", f"{len(origins)} x {len(destinations)} = {len(origins) * len(destinations)} pairs"
            )
        departure = parse_local_time(departure_time, "departureTime")
        parsed_origins = [parse_stop(s) for s in origins]
        parsed_destinations = [parse_stop(s) for s in destinations]
        pairs = [
            (i, j)
            for i in range(len(parsed_origins))
            for j in range(len(parsed_destinations))
            if parsed_origins[i] != parsed_destinations[j]
        ]
        uncached = sum(
            1
            for i, j in pairs
            if self._cache.get(("route", (parsed_origins[i], parsed_destinations[j]), mode, departure, None)) is None
        )
        if uncached > self.state.remaining():
            raise MapsError("budget_exhausted", log_detail=f"matrix needs {uncached}")
        elements = []
        for i, origin in enumerate(parsed_origins):
            for j, destination in enumerate(parsed_destinations):
                elements.append(await self._element(i, j, origin, destination, mode, departure))
        return {"travelMode": travel_mode, "elements": elements}

    async def _element(
        self,
        i: int,
        j: int,
        origin: str | tuple[float, float],
        destination: str | tuple[float, float],
        mode: str,
        departure: dt.datetime | None,
    ) -> dict[str, Any]:
        element: dict[str, Any] = {
            "originIndex": i,
            "destinationIndex": j,
            "status": None,
            "condition": None,
            "distanceMeters": None,
            "duration": None,
            "staticDuration": None,
            "localizedValues": None,
        }
        if origin == destination:
            element.update(condition="ROUTE_EXISTS", distanceMeters=0, duration="0s", staticDuration="0s")
            return element
        result = await self._directions([origin, destination], mode, departure, None)
        if result.status == "NOT_FOUND":
            which = "origin" if 0 in result.unresolved else "destination"
            element["status"] = {"code": "NOT_FOUND", "message": f"{which} not found"}
            return element
        if not result.routes:
            element["condition"] = "ROUTE_NOT_FOUND"
            return element
        best = shapes.route(result.routes[0], result.waypoints)
        element.update(
            condition="ROUTE_EXISTS",
            distanceMeters=best["distanceMeters"],
            duration=best["duration"],
            staticDuration=best["staticDuration"],
            localizedValues=best["localizedValues"],
        )
        return element

    # -- multi-stop planning ----------------------------------------------------------------

    async def plan_route(
        self,
        origin: str,
        stops: list[str],
        destination: str | None,
        round_trip: bool,
        travel_mode: str,
        departure_time: str | None,
        dwell_minutes: list[int] | None,
        optimize_order: bool,
    ) -> tuple[dict[str, Any], Route, list[DirectionsWaypoint]]:
        """Order the stops (optionally), route them and time the visit. Returns the plan, the
        chosen route and its waypoints (for steps and the map)."""
        mode = shapes.TRAVEL_MODES.get(travel_mode)
        if mode is None:
            raise invalid("travelMode must be DRIVE, WALK or BICYCLE")
        if len(stops) > MAX_PLAN_STOPS:
            raise invalid(f"give at most {MAX_PLAN_STOPS} stops")
        if not stops and not destination and not round_trip:
            raise invalid("give stops, a destination or both")
        if destination and round_trip:
            raise invalid("give a destination or roundTrip, not both")
        dwell = list(dwell_minutes or [])
        if len(dwell) > len(stops) or any(not 0 <= d <= 24 * 60 for d in dwell):
            raise invalid("dwellMinutes: one value (0-1440) per stop, in the order given")
        dwell += [0] * (len(stops) - len(dwell))
        departure = parse_local_time(departure_time, "departureTime")

        first = [parse_stop(origin), *(parse_stop(s) for s in stops)]
        if destination:
            first.append(parse_stop(destination))
        elif round_trip:
            first.append(first[0])
        given = await self._directions(first, mode, departure, None)
        if given.status == "NOT_FOUND":
            names = ", ".join(f"stop {i + 1} ('{given.waypoints[i].query[:80]}')" for i in given.unresolved)
            raise MapsError("waypoint_not_found", f"Not found: {names}")

        # Re-route by what Google resolved, so every candidate visits exactly the same places.
        exact = [w.hex_id or (w.latitude, w.longitude) for w in given.waypoints]
        points = [(w.latitude or 0.0, w.longitude or 0.0) for w in given.waypoints]
        n = len(stops)
        end_point = points[-1] if (destination or round_trip) else None
        orders = [tuple(range(n))]
        if optimize_order and n >= 2:
            # Ranking every order of up to 8 stops takes a fraction of a second: off the event loop.
            orders = await asyncio.to_thread(planner.candidate_orders, points[0], points[1 : n + 1], end_point)

        def seconds_of(result: Directions) -> int | None:
            return result.routes[0].duration_seconds if result.routes else None

        evaluated: list[tuple[int, tuple[int, ...], Directions]] = []
        for order in orders:
            if order == tuple(range(n)):
                result = given
            else:
                stops_exact = [exact[0], *(exact[1 + i] for i in order)]
                if destination or round_trip:
                    stops_exact.append(exact[-1])
                result = await self._directions(stops_exact, mode, departure, None)
            total = seconds_of(result)
            if total is not None:
                evaluated.append((total, order, result))
        if not evaluated:
            hint = "South Korea has no Google walking or driving routes (use Naver Map or KakaoMap)"
            raise MapsError("route_not_found", hint if _in_korea(given) else None)
        evaluated.sort(key=lambda item: item[0])
        best_seconds, best_order, best = evaluated[0]
        given_seconds = seconds_of(given)
        route = best.routes[0]
        waypoints = best.waypoints

        # Opening hours and the time zone, from each place's details (cached).
        records: list[ParsedPlace | None] = []
        for w in waypoints:
            record = None
            if w.place_id.startswith("ChIJ"):
                try:
                    record = await self._place_record(w.place_id)
                except MapsError as exc:
                    if exc.code in ("blocked", "budget_exhausted"):
                        raise
            records.append(record)
        zone = next((shapes._zone(r.timezone) for r in records if r is not None and shapes._zone(r.timezone)), None)
        if departure is not None:
            start = departure.replace(tzinfo=zone) if zone else None
        else:
            start = dt.datetime.fromtimestamp(self._clock(), zone).replace(second=0, microsecond=0) if zone else None

        def minute(moment: dt.datetime | None) -> str | None:
            """Clock times to the minute, as a person reads them."""
            if moment is None:
                return None
            return (moment + dt.timedelta(seconds=30)).replace(second=0, microsecond=0).isoformat()

        # Legs carry typical times; scale them by the route's traffic factor (driving).
        static = route.static_duration_seconds or 0
        factor = (route.duration_seconds or 0) / static if static else 1.0
        dwell_by_stop = [dwell[i] for i in best_order]
        elapsed = 0
        visits: list[dict[str, Any]] = []
        labels = iter(staticmap.LABELS)
        for index, waypoint in enumerate(waypoints):
            label = next(labels, "") if not (round_trip and index == len(waypoints) - 1) else "A"
            if index == 0:
                role, stay = "origin", 0
            elif index <= n:
                role, stay = "stop", dwell_by_stop[index - 1]
            else:
                role, stay = ("origin" if round_trip else "destination"), 0
            if index > 0:
                leg = route.legs[index - 1] if index - 1 < len(route.legs) else None
                elapsed += round((leg.duration_seconds or 0) * factor) if leg else 0
            arrive = start + dt.timedelta(seconds=elapsed) if start else None
            leave_elapsed = elapsed + stay * 60
            leave = start + dt.timedelta(seconds=leave_elapsed) if start else None
            record = records[index]
            status = None
            if role == "stop" and record is not None and arrive is not None:
                status = shapes.visit_status(record, arrive, leave or arrive)
            visits.append(
                {
                    "label": label,
                    "role": role,
                    "inputIndex": (best_order[index - 1] if role == "stop" else None),
                    **shapes.waypoint(waypoint),
                    "arrivalOffset": shapes.seconds(elapsed) if index > 0 else None,
                    "arrivalTime": minute(arrive) if index > 0 else None,
                    "dwellMinutes": stay,
                    "departureTime": minute(leave)
                    if role != "destination" and not (round_trip and index == len(waypoints) - 1)
                    else None,
                    "openingStatus": status,
                    "rating": record.rating if record else None,
                    "category": record.categories[0] if record and record.categories else None,
                }
            )
            elapsed = leave_elapsed
        legs = [
            {
                "from": visits[i]["label"],
                "to": visits[i + 1]["label"],
                "distanceMeters": leg.distance_meters,
                "duration": shapes.seconds(round((leg.duration_seconds or 0) * factor)),
                "staticDuration": shapes.seconds(leg.duration_seconds),
                "localizedValues": {"distance": shapes._text(leg.distance_text)},
            }
            for i, leg in enumerate(route.legs[: len(visits) - 1])
        ]
        warnings = [
            f"{v['label']} {v['displayName'] or v['query']}: {v['openingStatus']['text']}"
            for v in visits
            if v["openingStatus"] and v["openingStatus"]["state"] in ("closed", "closes_during_visit")
        ]
        plan = {
            "travelMode": travel_mode,
            "optimized": optimize_order and n >= 2,
            "order": list(best_order),
            "ordersCompared": len(evaluated),
            "savedVsGivenOrder": (
                shapes.seconds(given_seconds - best_seconds)
                if given_seconds is not None and best_order != tuple(range(n))
                else None
            ),
            "description": route.summary or None,
            "distanceMeters": route.distance_meters,
            "duration": shapes.seconds(route.duration_seconds),
            "durationWithDwell": shapes.seconds(elapsed),
            "departureTime": minute(start),
            "arrivalTime": visits[-1]["arrivalTime"],
            "timeZone": {"id": str(zone)} if zone else None,
            "stops": visits,
            "legs": legs,
            "warnings": warnings,
            "googleMapsUri": shapes.directions_uri(waypoints, mode),
        }
        return plan, route, waypoints

    # -- cards (MCP Apps views) ---------------------------------------------------------------

    async def show_route(
        self,
        origin: str,
        destination: str | None,
        intermediates: list[str] | None,
        travel_mode: str,
        departure_time: str | None,
        dwell_minutes: list[int] | None,
        optimize_order: bool,
        round_trip: bool,
        title: str | None,
        note: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The route card: the plan for the model, the line and the turns for the view only."""
        plan, route, _ = await self.plan_route(
            origin,
            intermediates or [],
            destination,
            round_trip,
            travel_mode,
            departure_time,
            dwell_minutes,
            optimize_order,
        )
        steps = [
            [
                {"i": s.instruction, "m": s.maneuver, "d": s.distance_text, "t": s.duration_text, "n": s.notices}
                for seg in leg.segments
                for s in seg.steps
            ]
            for leg in route.legs
        ]
        view = {"path": simplify(route.path, 800), "steps": steps}
        return {"kind": "route", "title": title, "note": note, **plan}, view

    async def show_places(
        self, items: list[dict[str, Any]], title: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Place cards: the facts for the model, the photos for the view only."""
        now = self._now()
        places, photos = [], {}
        labels = iter(staticmap.LABELS)
        for item in items:
            place_id = str(item.get("id") or "").strip()
            if place_id_to_hex_id(place_id) is None:
                raise invalid(f"'{place_id[:40]}' is not a place id (ChIJ…) from maps_search_places")
            record = await self._place_record(place_id)
            shaped = shapes.place(record, now, self.language)
            hours = shaped["currentOpeningHours"] or {}
            today = (record.opening_hours or [{}])[0]
            name = shaped["displayName"]["text"] if shaped["displayName"] else ""
            places.append(
                {
                    "id": shaped["id"],
                    "label": next(labels, ""),
                    "displayName": name or None,
                    "category": shaped["primaryTypeDisplayName"]["text"] if shaped["primaryTypeDisplayName"] else None,
                    "rating": shaped["rating"],
                    "userRatingCount": shaped["userRatingCount"],
                    "priceRangeText": shaped["priceRangeText"],
                    "formattedAddress": shaped["formattedAddress"],
                    "location": shaped["location"],
                    "phone": shaped["internationalPhoneNumber"] or shaped["nationalPhoneNumber"],
                    "websiteUri": shaped["websiteUri"],
                    "googleMapsUri": shaped["googleMapsUri"],
                    "directionsUri": "https://www.google.com/maps/dir/?"
                    + urlencode({"api": "1", "destination": name or place_id, "destination_place_id": place_id}),
                    "openNow": hours.get("openNow"),
                    "hoursStatusText": shaped["hoursStatusText"],
                    "todayHours": ", ".join(today.get("text") or []) or None,
                    "holidays": hours.get("holidays") or [],
                    "note": item.get("note") or None,
                }
            )
            if record.thumbnail:
                photos[shaped["id"]] = record.thumbnail
        return {"kind": "places", "title": title, "places": places}, {"photos": photos}

    # -- static maps ------------------------------------------------------------------------

    async def _locate(self, text: str) -> dict[str, Any]:
        """A centre or pin: 'lat,lng', a place id, or text (Google's first search result)."""
        stop = parse_stop(text)
        if isinstance(stop, tuple):
            return {"query": text, "displayName": None, "location": {"latitude": stop[0], "longitude": stop[1]}}
        if place_id_to_hex_id(stop) or hex_id_to_place_id(stop):
            found = await self.get_place(stop)
        else:
            places = (await self.search_places(stop, page_size=1))["places"]
            if not places or not places[0]["location"]:
                raise MapsError("waypoint_not_found", f"Not found: '{stop[:80]}'")
            found = places[0]
        name = (found.get("displayName") or {}).get("text")
        return {"query": text, "displayName": name, "location": found["location"], "placeId": found.get("id")}

    async def static_map(
        self,
        center: str | None,
        zoom: int | None,
        size: str,
        maptype: str,
        markers: list[str] | None,
        route: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bytes]:
        match = re.fullmatch(r"\s*(\d{2,4})\s*x\s*(\d{2,4})\s*", size or "")
        if not match:
            raise invalid("size must be WIDTHxHEIGHT, e.g. 640x480")
        width, height = int(match.group(1)), int(match.group(2))
        if not (128 <= width <= 1024 and 128 <= height <= 1024):
            raise invalid("width and height must be between 128 and 1024")
        if maptype not in staticmap.MAP_TYPES:
            raise invalid("maptype must be roadmap, satellite, hybrid or terrain")
        if not center and not markers and not route:
            raise invalid("give a center, markers or a route")
        spec = staticmap.MapSpec(width=width, height=height, maptype=maptype, zoom=zoom)
        legend_pins: list[dict[str, Any]] = []
        route_info = None
        labels = iter(staticmap.LABELS)
        if route:
            mode = shapes.TRAVEL_MODES.get(route.get("travelMode") or "DRIVE")
            if mode is None:
                raise invalid("route.travelMode must be DRIVE, WALK or BICYCLE")
            stops = [
                parse_stop(route.get("origin") or ""),
                *(parse_stop(s) for s in route.get("intermediates") or []),
                parse_stop(route.get("destination") or ""),
            ]
            result = await self._directions(stops, mode, None, None)
            if result.status == "NOT_FOUND":
                names = ", ".join(f"stop {i + 1} ('{result.waypoints[i].query[:80]}')" for i in result.unresolved)
                raise MapsError("waypoint_not_found", f"Not found: {names}")
            best = result.routes[0] if result.routes else None
            if best is not None:
                spec.path = list(best.path)
            for waypoint in result.waypoints:
                label = next(labels, "")
                spec.pins.append(staticmap.Pin(waypoint.latitude or 0.0, waypoint.longitude or 0.0, label))
                legend_pins.append({"label": label, **shapes.waypoint(waypoint)})
            route_info = {
                "travelMode": route.get("travelMode") or "DRIVE",
                "condition": "ROUTE_EXISTS" if best else "ROUTE_NOT_FOUND",
                "description": best.summary if best else None,
                "distanceMeters": best.distance_meters if best else None,
                "duration": shapes.seconds(best.duration_seconds) if best else None,
                "googleMapsUri": shapes.directions_uri(result.waypoints, mode),
            }
        for text in markers or []:
            found = await self._locate(text)
            label = next(labels, "")
            loc = found["location"]
            spec.pins.append(staticmap.Pin(loc["latitude"], loc["longitude"], label))
            legend_pins.append({"label": label, **found})
        if center:
            loc = (await self._locate(center))["location"]
            spec.center = (loc["latitude"], loc["longitude"])
        rendered = await staticmap.render(spec, self.google.tile)
        if rendered.tiles and rendered.missing_tiles == rendered.tiles:
            raise MapsError("upstream_error", log_detail="no map tiles")
        legend = {
            "center": {"latitude": round(rendered.center[0], 6), "longitude": round(rendered.center[1], 6)},
            "zoom": rendered.zoom,
            "size": f"{rendered.width}x{rendered.height}",
            "maptype": maptype,
            "markers": legend_pins,
            "route": route_info,
            "missingTiles": rendered.missing_tiles,
            "attribution": "Map data © Google",
        }
        return legend, rendered.image

    # -- status and canary ------------------------------------------------------------------

    def record_error(self, code: str) -> None:
        self.state.record_error(code)

    async def status(self, check: bool) -> dict[str, Any]:
        canary = await self.canary() if check else None
        out = {"version": __version__, **self.state.describe(), "cache_entries": len(self._cache)}
        if canary is not None:
            out["canary"] = canary
        return out

    async def canary(self) -> dict[str, Any]:
        """One real route, one place parse and one map tile; verdict cached 55 min (transient
        failures: 5 min)."""
        now = self._clock()
        last = self._canary
        if last is not None:
            ttl = CANARY_RETRY_S if last["code"] in TRANSIENT else CANARY_TTL_S
            if now - last["_at"] < ttl:
                return {k: v for k, v in last.items() if not k.startswith("_")} | {"cached": True}
        code = "ok"
        try:
            origin, place = self._canary_places
            walk = await self.google.route([origin, place], "walk")
            if walk.status != "OK" or not walk.routes or not walk.routes[0].duration_seconds:
                code = "route_empty"
            else:
                landmark = await self.google.place(place)
                if landmark is None or not landmark.name or landmark.latitude is None:
                    code = "place_empty"
                elif not landmark.opening_hours:
                    code = "hours_empty"
                else:
                    # One map tile around the place (cached a day): the path maps_static_map uses.
                    x, y = staticmap.world_px(landmark.latitude, landmark.longitude or 0.0, 15)
                    if not await self.google.tile("m", 15, int(x // staticmap.TILE), int(y // staticmap.TILE)):
                        code = "tiles_empty"
        except MapsError as exc:
            code = exc.code
        self._canary = {"ok": code == "ok", "code": code, "checked_at": iso(now), "_at": now}
        if code != "ok":
            log.warning("canary failed: %s", code)
        return {"ok": code == "ok", "code": code, "checked_at": iso(now), "cached": False}
