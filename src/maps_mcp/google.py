"""The only code that talks to Google: the vendored gmaps-scraper client, one request at a time.

Every call checks the breaker, spends the daily budget, waits for the single in-flight slot and
maps the library's exceptions to our error codes. The client is recycled every 15 minutes (the
library's session-age advice) and after a failure; consent cookies persist in the state dir, so a
recycle costs no extra request.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from .config import Settings
from .errors import MapsError
from .state import StateStore
from .vendor.gmaps import GMapsClient
from .vendor.gmaps.exceptions import AuthError, HostNotAllowedError, NetworkError, ParseError, RateLimitError
from .vendor.gmaps.exceptions import TimeoutError as GMapsTimeoutError
from .vendor.gmaps.rpc.directions import Directions
from .vendor.gmaps.rpc.parser import ParsedPlace

log = logging.getLogger(__name__)
T = TypeVar("T")

CLIENT_MAX_AGE_S = 15 * 60
# Map tiles: the endpoint google.com/maps itself draws from (256 px PNG/JPEG).
TILE_URL = "https://www.google.com/maps/vt"
TILE_CACHE_ENTRIES = 1500
TILE_CACHE_TTL_S = 24 * 3600
TILE_CONCURRENCY = 4
TILE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
SLOT_WAIT_S = 30.0
# The cookie warm-up when no saved consent cookies exist: google.com, two consent pages, /maps.
WARMUP_REQUESTS = 4

ClientFactory = Callable[[Settings], Any]


def default_client_factory(settings: Settings) -> GMapsClient:
    return GMapsClient(
        enrich=False,
        timeout=settings.request_timeout_s,
        max_retries=2,
        retry_delay=1.0,
        min_delay=settings.min_interval_s,
        language=settings.language,
        cookie_file=str(settings.cookie_file),
        validate=False,
    )


class GoogleMaps:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        *,
        client_factory: ClientFactory = default_client_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._state = state
        self._factory = client_factory
        self._clock = clock
        self._client: Any = None
        self._opened_at = 0.0
        self._slot = asyncio.Lock()
        self._tile_http: httpx.AsyncClient | None = None
        self._tile_gate = asyncio.Semaphore(TILE_CONCURRENCY)
        self._tiles: OrderedDict[tuple[str, int, int, int], tuple[float, bytes]] = OrderedDict()

    # -- calls ----------------------------------------------------------------------------

    async def search(
        self, query: str, latitude: float | None, longitude: float | None, radius_m: int | None, page_size: int
    ) -> list[ParsedPlace]:
        async def call(client: Any) -> list[ParsedPlace]:
            kwargs: dict[str, Any] = {"max_results": page_size}
            if latitude is not None and longitude is not None:
                kwargs.update(latitude=latitude, longitude=longitude)
                if radius_m:
                    kwargs["viewport_dist"] = float(max(radius_m * 2, 500))
            result = await client.search.places(query, **kwargs)
            return list(result.places)

        return await self._run(call)

    async def place(self, place_id: str) -> ParsedPlace | None:
        async def call(client: Any) -> ParsedPlace | None:
            return await client.places.get(place_id)

        return await self._run(call)

    async def route(
        self,
        waypoints: list[str | tuple[float, float]],
        mode: str,
        departure: dt.datetime | None = None,
        arrival: dt.datetime | None = None,
    ) -> Directions:
        async def call(client: Any) -> Directions:
            return await client.directions.route(waypoints, mode=mode, departure_time=departure, arrival_time=arrival)

        return await self._run(call)

    async def tile(self, layer: str, zoom: int, x: int, y: int) -> bytes | None:
        """One map tile, or None when Google has none (the map shows a gap). Budgeted and cached;
        a 429 or a /sorry redirect opens the breaker like any other Google pushback."""
        key = (layer, zoom, x, y)
        cached = self._tiles.get(key)
        if cached is not None and cached[0] > self._clock():
            self._tiles.move_to_end(key)
            return cached[1]
        self._state.check_breaker()
        self._state.take(1, kind="tiles")
        async with self._tile_gate:
            try:
                response = await self._tile_client().get(
                    TILE_URL,
                    params={"lyrs": layer, "hl": self._settings.language, "x": x, "y": y, "z": zoom},
                )
            except httpx.HTTPError as exc:
                log.info("tile request failed (%s)", type(exc).__name__)
                return None
        location = response.headers.get("location", "")
        if response.status_code == 429 or "/sorry" in location:
            until = self._state.trip_breaker()
            raise MapsError("blocked", log_detail=f"tile {response.status_code}; breaker until {until:.0f}")
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        self._tiles[key] = (self._clock() + TILE_CACHE_TTL_S, response.content)
        while len(self._tiles) > TILE_CACHE_ENTRIES:
            self._tiles.popitem(last=False)
        return response.content

    def _tile_client(self) -> httpx.AsyncClient:
        if self._tile_http is None:
            self._tile_http = httpx.AsyncClient(
                headers={"User-Agent": TILE_USER_AGENT, "Accept": "image/avif,image/webp,image/png,image/*"},
                timeout=self._settings.request_timeout_s,
                follow_redirects=False,
            )
        return self._tile_http

    # -- plumbing -------------------------------------------------------------------------

    async def _run(self, call: Callable[[Any], Awaitable[T]]) -> T:
        self._state.check_breaker()
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=SLOT_WAIT_S)
        except TimeoutError:
            raise MapsError("busy", log_detail="slot wait timed out") from None
        try:
            self._state.check_breaker()
            client = await self._ensure_client()
            self._state.take(1)
            try:
                return await call(client)
            except MapsError:
                raise
            except Exception as exc:
                await self._drop_client()
                raise self._classify(exc) from None
        finally:
            self._slot.release()

    def _classify(self, exc: Exception) -> MapsError:
        name = type(exc).__name__
        if isinstance(exc, (RateLimitError, AuthError)):
            until = self._state.trip_breaker()
            return MapsError("blocked", log_detail=f"{name}; breaker until {until:.0f}")
        if isinstance(exc, ParseError):
            return MapsError("schema_changed", log_detail=name)
        if isinstance(exc, GMapsTimeoutError):
            return MapsError("upstream_error", log_detail="timeout")
        if isinstance(exc, NetworkError):
            original = exc.original_error
            if isinstance(original, httpx.HTTPStatusError) and 400 <= original.response.status_code < 500:
                # Google refusing our request shape (400/404) means the protocol moved.
                return MapsError("schema_changed", log_detail=f"HTTP {original.response.status_code}")
            return MapsError("upstream_error", log_detail=name)
        if isinstance(exc, HostNotAllowedError):
            return MapsError("upstream_error", log_detail="redirect to another host refused")
        if isinstance(exc, json.JSONDecodeError):
            return MapsError("schema_changed", log_detail=name)
        # Any other ValueError comes from parsing Google's data, never from the caller (their
        # arguments are checked before a request): an internal error, with no text passed on.
        log.error("unexpected %s from gmaps", name)
        return MapsError("internal_error", log_detail=name)

    async def _ensure_client(self) -> Any:
        if self._client is not None and self._clock() - self._opened_at < CLIENT_MAX_AGE_S:
            return self._client
        await self._drop_client()
        if not self._settings.cookie_file.exists():
            self._state.take(WARMUP_REQUESTS)
        client = self._factory(self._settings)
        try:
            await client.__aenter__()
        except Exception as exc:
            raise self._classify(exc) from None
        self._client = client
        self._opened_at = self._clock()
        return client

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:  # closing must never mask the real outcome
                log.info("closing gmaps client failed (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        await self._drop_client()
        if self._tile_http is not None:
            await self._tile_http.aclose()
            self._tile_http = None
