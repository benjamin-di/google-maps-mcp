"""maps-mcp: a read-only MCP server for Google Maps travel times, routes and places.

Data comes from the requests google.com/maps makes when signed out (no key, no account),
through a vendored copy of gmaps-scraper (src/maps_mcp/vendor/gmaps, see NOTICE). Safety
boundary (here, in ``google`` and in ``tests/test_safety.py``): Google is the only host, calls
are paced, budgeted and behind a breaker, and tool results never carry exception text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Awaitable
from importlib import resources
from typing import Annotated, Any, Literal, TypeVar

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ArgumentError
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, logs
from .auth import AuthError, bearer_auth_from_env
from .config import ConfigError, Settings
from .errors import AGENT_INPUT_CODES, NEXT_STEP, MapsError
from .google import GoogleMaps
from .service import MAX_MATRIX_ELEMENTS, MAX_PLAN_STOPS, MapsService
from .state import StateStore

log = logging.getLogger("maps")
T = TypeVar("T")

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

# The cards (MCP Apps): claude.ai and ChatGPT draw maps_show_route / maps_show_places with this page in a sandboxed
# frame. It loads map tiles from www.google.com and place photos from Google's photo hosts, connects nowhere, and opens
# links through the host. Exact hosts only: claude.ai has skipped views whose CSP had wildcards or empty lists.
VIEW_URI = "ui://maps/view.html"
VIEW_CSP = {
    "resourceDomains": [
        "https://www.google.com",
        "https://lh3.googleusercontent.com",
        "https://lh4.googleusercontent.com",
        "https://lh5.googleusercontent.com",
        "https://lh6.googleusercontent.com",
    ]
}
VIEW_META = "maps/view"  # the card's line, turns and photos ride in the result's _meta: the model is not sent them
VIEW_HTML = (resources.files(__package__) / "view.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)
# Tool and resource lists change only on deploy; MCP 2026-07-28 clients may cache them for 10 minutes.
LIST_CACHE_TTL = 600

HTTP_OPTIONS: dict[str, Any] = {
    "stateless_http": True,
    "json_response": True,
    # Host/Origin checks on loopback binds (DNS rebinding), not on 0.0.0.0 behind a proxy.
    "host_origin_protection": "auto",
}

Stop = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="A place name or address ('Albertina, Vienna'), a place id (ChIJ…) or 'lat,lng'",
    ),
]
TravelMode = Annotated[Literal["DRIVE", "WALK", "BICYCLE"], Field(description="DRIVE (default), WALK or BICYCLE")]
LocalTime = Annotated[
    str | None,
    Field(max_length=32, description="Local wall-clock time at the stop, e.g. 2026-10-22T08:30 (no offset)"),
]


class PlacePick(BaseModel):
    """A place to show as a card."""

    id: Annotated[str, Field(min_length=6, max_length=64, description="Place id (ChIJ…) from maps_search_places")]
    note: Annotated[str | None, Field(max_length=200, description="Your one-line reason for this place")] = None


class RouteSpec(BaseModel):
    """A route to draw on a static map (same stops as maps_compute_route)."""

    origin: Stop
    destination: Stop
    intermediates: list[Stop] | None = Field(default=None, max_length=8)
    travelMode: TravelMode = "DRIVE"


_ERRORS = "\n".join(f"- {code}: {text}" for code, text in NEXT_STEP.items())

INSTRUCTIONS = f"""\
Travel times and places from Google Maps (the public google.com/maps web page, signed out).
Read-only; nothing here books, saves or changes anything.

Use it to answer "how long from A to B" (walking, driving, cycling) and "what is this place,
where is it, when is it open". Results use the field names of Google's official Places API
(New) and Routes API, so they read like those APIs' JSON.

Tools:
- maps_search_places(textQuery, latitude/longitude/radiusMeters to bias, pageSize): find
  places; each has id, displayName, formattedAddress, location, rating, userRatingCount,
  phone, websiteUri, timeZone and currentOpeningHours (next 7 days, dated).
- maps_get_place(id): one place by its id (ChIJ…), with the same fields.
- maps_compute_route(origin, destination, intermediates, travelMode, departureTime |
  arrivalTime, computeAlternativeRoutes): duration and distance, per leg between stops.
  DRIVE gives duration in traffic (now, or predicted for the time you give), staticDuration
  (typical), durationRange (e.g. "26–50 min") and the arrival/departure time.
  googleMapsUri opens the same route on a phone.
  includeSteps adds the turn-by-turn directions per leg (steps with instructions such as
  "Turn left onto Gertrude-Fröhlich-Sandner-Straße", maneuver, distance, time, notices like
  "Toll road") and the grouped segments Maps lists ("Get on A4"); includePolyline adds the
  route line.
- maps_compute_route_matrix(origins, destinations, travelMode, departureTime): durations between up to
  {MAX_MATRIX_ELEMENTS} pairs, e.g. to order a day's stops.
- maps_static_map(center, zoom, size, maptype, markers, route): a map image (like the Static
  Maps API) of an area around a place, lettered pins (A, B, …) and/or a route drawn in blue,
  from Google's own map tiles; roadmap, satellite, hybrid or terrain. Without center/zoom it
  fits the pins and route. Returns the JPEG plus a legend (labels to places, route summary).
- maps_plan_route(origin, stops, destination | roundTrip, travelMode, departureTime,
  dwellMinutes, optimizeOrder): a multi-stop plan. With optimizeOrder it finds the best order
  of up to {MAX_PLAN_STOPS} stops (straight-line ranking of every order, then real Google routes for the best
  few and the given order), and times the visit: arrival per stop, stays, and whether each
  stop is open then (openingStatus: open, closes_during_visit, closed, unknown) with warnings.
- maps_show_route(...same as plan...): the same, shown to the person as a card: an
  interactive map (route, lettered pins, map/satellite), the stops with arrival times and
  open/closed status, turn-by-turn steps per leg and an "Open in Google Maps" button.
- maps_show_places(items [{{id, note}}], title): place cards with photo, rating, open now,
  today's hours, holidays, address, phone, website, directions, and a map with lettered pins.
- maps_status(check): budget, breaker and last error; check=true runs the live canary.

Presenting: whenever you give someone a route, directions or a day's stops, call
maps_show_route; whenever you recommend or compare places, call maps_show_places with the
picks and a one-line note each. Do it without being asked, then add only what the card does
not show. claude.ai and ChatGPT draw the cards; other clients get the same data as JSON.

Stops: text is geocoded by Google and can pick the wrong place for a short or ambiguous
name; for exact stops pass the id from maps_search_places, or 'lat,lng'. Each route lists
the resolved waypoints: check them. Times are local wall-clock time at the stop.

Opening hours: currentOpeningHours has periods with dates, weekdayDescriptions, openNow,
nextOpenTime/nextCloseTime (in the place's time zone), and holidays: the holiday Google
attaches to a date with confirmed=false when Maps only says it "might affect these hours".
Treat Google's hours as a good secondary source, not proof.

Durations are Google's "<seconds>s" strings; distances are meters. Leg durations are typical
(no traffic). Every key is present; null means Google did not say. Fields not in Google's
APIs: categories, holidays, hoursStatusText, priceRangeText, durationRange,
durationWithoutTraffic, departureTime/arrivalTime, waypoints, condition, note.

Google limits: no walking or driving routes in South Korea (condition ROUTE_NOT_FOUND with
a note); data in mainland China is thin. Requests to Google go one at a time, about one a
second (map tiles a few at once), within a daily budget: avoid loops; reuse results (they are
cached for a while).

Errors come as {{"error": {{"code", "next_step", "retryable"}}}}:
{_ERRORS}
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _argument_detail(error: PydanticValidationError) -> str:
    """Name each bad argument and the rule it broke; never echo the input value."""
    parts = []
    for item in error.errors()[:3]:
        where = ".".join(str(step) for step in item.get("loc", ()) if step != "arguments") or "arguments"
        parts.append(f"{where}: {item.get('msg', 'invalid')}")
    return "; ".join(parts)[:300] or "invalid arguments"


class TypedArgumentErrors(Middleware):
    """Argument-validation failures as the typed ``invalid_argument`` error every other failure uses."""

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        try:
            return await call_next(context)
        except ArgumentError as exc:
            cause = exc.__cause__
            detail = _argument_detail(cause) if isinstance(cause, PydanticValidationError) else "invalid arguments"
            raise ToolError(
                _json({"error": MapsError("invalid_argument", detail).payload()}), log_level=logging.INFO
            ) from None


def build_service(settings: Settings) -> MapsService:
    state = StateStore(
        settings.state_dir,
        request_budget=settings.daily_request_budget,
        tile_budget=settings.daily_tile_budget,
        budget_tz=settings.budget_tz,
    )
    return MapsService(
        GoogleMaps(settings, state),
        state,
        language=settings.language,
        canary=(settings.canary_from, settings.canary_to),
    )


def create_server(service: MapsService | None = None, *, auth: Any = None) -> FastMCP:
    """Build the FastMCP server. ``service``/``auth`` are injectable for tests."""
    svc = service or build_service(Settings.from_env())
    mcp = FastMCP(
        "maps",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=auth,
        mask_error_details=True,
        cache_ttl=LIST_CACHE_TTL,
    )
    mcp.add_middleware(logs.ToolCallLog())
    mcp.add_middleware(TypedArgumentErrors())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        # Liveness only: never calls Google.
        return JSONResponse({"status": "healthy"})

    async def call(awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except MapsError as exc:
            log.info("tool error %s (%s)", exc.code, exc.log_detail or "-")
            svc.record_error(exc.code)
            level = logging.INFO if exc.code in AGENT_INPUT_CODES else logging.WARNING
            raise ToolError(_json({"error": exc.payload()}), log_level=level) from None
        except Exception as exc:  # never echo exception text to the model
            log.error("tool crashed: %s", type(exc).__name__)
            svc.record_error("internal_error")
            raise ToolError(_json({"error": MapsError("internal_error").payload()})) from None

    @mcp.tool(annotations=READ)
    async def maps_search_places(
        textQuery: Annotated[
            str, Field(min_length=1, max_length=200, description="What you'd type into Google Maps search")
        ],
        latitude: Annotated[float | None, Field(ge=-90, le=90, description="Bias results near here")] = None,
        longitude: Annotated[float | None, Field(ge=-180, le=180)] = None,
        radiusMeters: Annotated[int | None, Field(ge=100, le=50_000, description="Bias radius")] = None,
        pageSize: Annotated[int, Field(ge=1, le=20, description="Places to return (1-20)")] = 5,
    ) -> dict[str, Any]:
        """Find places like Google Maps search (Places API searchText shape): {places: [{id,
        displayName, formattedAddress, location, googleMapsUri, primaryTypeDisplayName,
        categories, rating, userRatingCount, internationalPhoneNumber, nationalPhoneNumber,
        websiteUri, priceLevel, priceRangeText, timeZone, currentOpeningHours,
        hoursStatusText}]}. Put the city in textQuery or pass latitude/longitude."""
        return await call(svc.search_places(textQuery, latitude, longitude, radiusMeters, pageSize))

    @mcp.tool(annotations=READ)
    async def maps_get_place(
        id: Annotated[str, Field(min_length=6, max_length=64, description="Place id (ChIJ…) from maps_search_places")],
    ) -> dict[str, Any]:
        """One place by id (Places API Place shape, same fields as maps_search_places), with
        currentOpeningHours for the next 7 days: dated periods, weekdayDescriptions, openNow,
        nextOpenTime/nextCloseTime and holidays."""
        return await call(svc.get_place(id))

    @mcp.tool(annotations=READ)
    async def maps_compute_route(
        origin: Stop,
        destination: Stop,
        intermediates: Annotated[
            list[Stop] | None, Field(max_length=8, description="Stops in between, in order (max 8)")
        ] = None,
        travelMode: TravelMode = "DRIVE",
        departureTime: LocalTime = None,
        arrivalTime: LocalTime = None,
        computeAlternativeRoutes: Annotated[bool, Field(description="Also return alternatives (max 3)")] = False,
        includeSteps: Annotated[bool, Field(description="Add turn-by-turn steps and segments per leg")] = False,
        includePolyline: Annotated[bool, Field(description="Add the route line (encoded polyline)")] = False,
    ) -> dict[str, Any]:
        """Travel time and distance through the stops in order (Routes API computeRoutes
        shape): {travelMode, routes: [{description, distanceMeters, duration, staticDuration,
        localizedValues, durationWithoutTraffic, durationRange, departureTime, arrivalTime,
        legs: [{distanceMeters, duration, staticDuration, startLocation, endLocation,
        localizedValues}]}], waypoints: [{query, displayName, formattedAddress, placeId,
        location}], googleMapsUri, condition, note}. duration is in traffic for DRIVE (now or
        at departureTime/arrivalTime), otherwise typical. includeSteps fills legs[].steps
        [{distanceMeters, staticDuration, startLocation, navigationInstruction {maneuver,
        instructions}, localizedValues, notices}] and legs[].segments [{instructions,
        distanceMeters, staticDuration, firstStepIndex, stepCount}]; includePolyline fills
        routes[].polyline {encodedPolyline}."""
        return await call(
            svc.compute_route(
                origin,
                destination,
                intermediates,
                travelMode,
                departureTime,
                arrivalTime,
                computeAlternativeRoutes,
                includeSteps,
                includePolyline,
            )
        )

    @mcp.tool(annotations=READ)
    async def maps_compute_route_matrix(
        origins: Annotated[list[Stop], Field(min_length=1, max_length=MAX_MATRIX_ELEMENTS)],
        destinations: Annotated[list[Stop], Field(min_length=1, max_length=MAX_MATRIX_ELEMENTS)],
        travelMode: TravelMode = "DRIVE",
        departureTime: LocalTime = None,
    ) -> dict[str, Any]:
        """Duration and distance for every origin x destination pair, at most 25 pairs; each
        pair is one Google request (Routes API computeRouteMatrix element shape): {travelMode,
        elements: [{originIndex, destinationIndex, status, condition, distanceMeters,
        duration, staticDuration, localizedValues}]}. Identical origin and destination cost
        nothing."""
        return await call(svc.compute_route_matrix(origins, destinations, travelMode, departureTime))

    @mcp.tool(annotations=READ)
    async def maps_static_map(
        center: Annotated[
            str | None, Field(max_length=200, description="Centre: a place or area name, a place id or 'lat,lng'")
        ] = None,
        zoom: Annotated[
            int | None, Field(ge=1, le=20, description="1 world … 10 city … 15 streets … 18 buildings")
        ] = None,
        size: Annotated[str, Field(max_length=9, description="WIDTHxHEIGHT, 128-1024 each")] = "640x480",
        maptype: Annotated[
            Literal["roadmap", "satellite", "hybrid", "terrain"], Field(description="Map style")
        ] = "roadmap",
        markers: Annotated[
            list[Stop] | None, Field(max_length=10, description="Places to pin, lettered after the route's stops")
        ] = None,
        route: Annotated[RouteSpec | None, Field(description="A route to draw, with its stops pinned A, B, …")] = None,
    ) -> ToolResult:
        """A map image like the Static Maps API: an area (center + zoom), pins and/or a route
        line, drawn on Google Maps tiles. Without center or zoom the view fits the pins and
        route. Returns a JSON legend {center, zoom, size, maptype, markers [{label, query,
        displayName, location, …}], route {travelMode, condition, description,
        distanceMeters, duration, googleMapsUri}, missingTiles, attribution} and the JPEG."""
        legend, jpeg = await call(
            svc.static_map(center, zoom, size, maptype, markers, route.model_dump() if route else None)
        )
        return ToolResult(
            content=[
                mcp_types.TextContent(type="text", text=_json(legend)),
                Image(data=jpeg, format="jpeg").to_image_content(),
            ],
            structured_content=legend,
        )

    @mcp.tool(annotations=READ)
    async def maps_plan_route(
        origin: Stop,
        stops: Annotated[
            list[Stop], Field(max_length=MAX_PLAN_STOPS, description=f"Places to visit (up to {MAX_PLAN_STOPS})")
        ],
        destination: Annotated[Stop | None, Field(description="Fixed end; omit to end at the last stop")] = None,
        roundTrip: Annotated[bool, Field(description="Come back to the origin")] = False,
        travelMode: TravelMode = "DRIVE",
        departureTime: LocalTime = None,
        dwellMinutes: Annotated[
            list[int] | None, Field(max_length=MAX_PLAN_STOPS, description="Minutes at each stop, in the order given")
        ] = None,
        optimizeOrder: Annotated[bool, Field(description="Find the fastest order of the stops")] = True,
    ) -> dict[str, Any]:
        """Plan a multi-stop route: {travelMode, optimized, order (indices of stops in the
        chosen order), ordersCompared, savedVsGivenOrder, description, distanceMeters,
        duration, durationWithDwell, departureTime, arrivalTime, timeZone, stops [{label,
        role, inputIndex, query, displayName, formattedAddress, placeId, location,
        arrivalOffset, arrivalTime, dwellMinutes, departureTime, openingStatus {state, text,
        hours}, rating, category}], legs [{from, to, distanceMeters, duration,
        staticDuration}], warnings, googleMapsUri}. Without departureTime the plan starts now."""

        async def run() -> dict[str, Any]:
            plan, _, _ = await svc.plan_route(
                origin, stops, destination, roundTrip, travelMode, departureTime, dwellMinutes, optimizeOrder
            )
            return plan

        return await call(run())

    @mcp.resource(
        VIEW_URI,
        name="maps-view",
        title="Google Maps cards",
        description="Route and place cards (interactive map, stops, steps, photos, hours) for maps_show_*.",
        mime_type="text/html;profile=mcp-app",
        app={"csp": VIEW_CSP, "prefersBorder": False},
    )
    def maps_view() -> str:
        return VIEW_HTML

    @mcp.tool(
        title="Show route",
        annotations=READ,
        app={"resourceUri": VIEW_URI, "visibility": ["model", "app"]},
    )
    async def maps_show_route(
        origin: Stop,
        destination: Annotated[Stop | None, Field(description="End; omit with stops to end at the last stop")] = None,
        intermediates: Annotated[
            list[Stop] | None, Field(max_length=MAX_PLAN_STOPS, description="Stops in between")
        ] = None,
        travelMode: TravelMode = "DRIVE",
        departureTime: LocalTime = None,
        dwellMinutes: Annotated[
            list[int] | None, Field(max_length=MAX_PLAN_STOPS, description="Minutes at each stop, in the order given")
        ] = None,
        optimizeOrder: Annotated[bool, Field(description="Reorder the stops for the fastest route")] = False,
        roundTrip: Annotated[bool, Field(description="Come back to the origin")] = False,
        title: Annotated[str | None, Field(max_length=80, description="Heading above the card")] = None,
        note: Annotated[str | None, Field(max_length=300, description="Your short note under the heading")] = None,
    ) -> ToolResult:
        """Show a route to the person as a card: interactive map with the route and lettered
        pins, total time and distance, the stops with arrival times and open/closed status,
        turn-by-turn steps per leg, and an "Open in Google Maps" button (claude.ai and ChatGPT
        draw it). Same facts as maps_plan_route. Use it whenever you give directions or a route."""

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
            return await svc.show_route(
                origin,
                destination,
                intermediates,
                travelMode,
                departureTime,
                dwellMinutes,
                optimizeOrder,
                roundTrip,
                title,
                note,
            )

        shown, view = await call(run())
        return ToolResult(structured_content=shown, meta={VIEW_META: view})

    @mcp.tool(
        title="Show places",
        annotations=READ,
        app={"resourceUri": VIEW_URI, "visibility": ["model", "app"]},
    )
    async def maps_show_places(
        items: Annotated[list[PlacePick], Field(min_length=1, max_length=10, description="Places to show, in order")],
        title: Annotated[str | None, Field(max_length=80, description="Heading above the cards")] = None,
    ) -> ToolResult:
        """Show places to the person as cards: photo, rating, category, open now, today's hours,
        holiday notices, address, phone, website, directions and Map links, your note, plus a
        map with lettered pins (claude.ai and ChatGPT draw them). Use it whenever you recommend
        or compare places; ids come from maps_search_places."""

        async def run() -> tuple[dict[str, Any], dict[str, Any]]:
            return await svc.show_places([i.model_dump() for i in items], title)

        shown, view = await call(run())
        return ToolResult(structured_content=shown, meta={VIEW_META: view})

    @mcp.tool(annotations=READ)
    async def maps_status(
        check: Annotated[bool, Field(description="Also run the live canary (cached 55 min)")] = False,
    ) -> dict[str, Any]:
        """Version, daily request and tile budgets left, breaker, last error and cache size.
        With check=true, also {canary: {ok, code, checked_at, cached}}: one real route, one
        place read and one map tile, cached 55 min (5 min after a transient failure); ok is
        false on any error. Never fails."""
        return await call(svc.status(check))

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="maps-mcp", description="Read-only MCP server for Google Maps travel times, routes and places."
    )
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="http: streamable HTTP on /mcp, bearer required (default); stdio: for a local MCP client",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MAPS_HOST", "127.0.0.1"),
        help="HTTP listen address (default $MAPS_HOST, else 127.0.0.1; the image sets MAPS_HOST=0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = parser.parse_args(argv)
    logs.setup("maps")
    try:
        if args.transport == "stdio":
            create_server().run(transport="stdio", show_banner=False)
            return
        # Fail closed: /mcp answers 401 without a configured bearer.
        auth = bearer_auth_from_env()
        server = create_server(auth=auth)
    except (AuthError, ConfigError) as exc:
        # Our own startup message (configuration), never a tool result.
        print("maps-mcp:", *exc.args, file=sys.stderr)
        raise SystemExit(1) from None
    server.run(transport="streamable-http", host=args.host, port=args.port, show_banner=False, **HTTP_OPTIONS)


if __name__ == "__main__":
    main()
