"""Place search and details as a Python library, without MCP.

``PlacesClient`` runs the same service as the server: the same shaped dicts as
``maps_search_places`` and ``maps_get_place``, the same pacing (one request at a time), daily
budget, breaker, cache and typed errors. Every failure is a ``MapsError`` whose ``code`` is one of
``ERROR_CODES``; bad constructor arguments raise ``ConfigError``.

    async with PlacesClient(state_dir="/var/lib/myapp/maps", daily_request_budget=200) as maps:
        found = await maps.search_places("Steirereck, Vienna", page_size=1)
        place = await maps.get_place(found["places"][0]["id"])
        place["reservationLinks"]  # [{"uri": "<the booking page>", "provider": "opentable.com"}]

This module's names and the result fields are a stable API under SemVer (README, "Use as a
library"). Keep one client per process and one process per state directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings
from .errors import NEXT_STEP, RETRYABLE, MapsError
from .service import MapsService, build_service

__all__ = ["ERROR_CODES", "RETRYABLE_CODES", "ConfigError", "MapsError", "PlacesClient"]

ERROR_CODES: frozenset[str] = frozenset(NEXT_STEP)
RETRYABLE_CODES: frozenset[str] = RETRYABLE


class PlacesClient:
    """Google Maps place search and details for one process.

    ``state_dir`` holds the budget, breaker and Google's consent cookies (default: the per-user
    state directory, ``$XDG_STATE_HOME/google-maps-mcp``). Give each process its own: the files
    are not shared safely between processes. Environment variables (``MAPS_*``) are not read.
    """

    def __init__(
        self,
        *,
        state_dir: str | os.PathLike[str] | None = None,
        daily_request_budget: int = 1000,
        budget_tz: str = "UTC",
        language: str = "en",
        min_interval_s: float = 1.0,
        request_timeout_s: float = 15.0,
    ) -> None:
        # The server's validation, fed from arguments instead of the environment.
        env = {
            "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME", ""),
            "MAPS_STATE_DIR": str(Path(state_dir)) if state_dir is not None else "",
            "MAPS_DAILY_REQUEST_BUDGET": str(daily_request_budget),
            "MAPS_BUDGET_TZ": budget_tz,
            "MAPS_LANGUAGE": language,
            "MAPS_MIN_INTERVAL_S": str(min_interval_s),
            "MAPS_REQUEST_TIMEOUT_S": str(request_timeout_s),
        }
        self.settings = Settings.from_env(env)
        self._service: MapsService = build_service(self.settings)

    async def search_places(
        self,
        text_query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_meters: int | None = None,
        page_size: int = 5,
    ) -> dict[str, Any]:
        """``{"places": [place, ...]}`` as ``maps_search_places`` returns it; one request."""
        if not 1 <= page_size <= 20:
            raise MapsError("invalid_argument", "page_size must be 1-20")
        if radius_meters is not None and not 100 <= radius_meters <= 50_000:
            raise MapsError("invalid_argument", "radius_meters must be 100-50000")
        if latitude is not None and not -90 <= latitude <= 90:
            raise MapsError("invalid_argument", "latitude must be -90 to 90")
        if longitude is not None and not -180 <= longitude <= 180:
            raise MapsError("invalid_argument", "longitude must be -180 to 180")
        if not 0 < len(text_query.strip()) <= 200:
            raise MapsError("invalid_argument", "text_query must be 1-200 characters")
        return await self._call(self._service.search_places(text_query, latitude, longitude, radius_meters, page_size))

    async def get_place(self, place_id: str) -> dict[str, Any]:
        """One place by id (``ChIJ…`` or ``0x…:0x…``) as ``maps_get_place`` returns it; one
        request, then cached for 6 hours."""
        return await self._call(self._service.get_place(place_id))

    def status(self) -> dict[str, Any]:
        """Today's budget, the breaker and the last error (no request)."""
        return self._service.state.describe()

    async def _call(self, awaitable: Any) -> dict[str, Any]:
        try:
            return await awaitable
        except MapsError as exc:
            self._service.record_error(exc.code)
            raise
        except Exception as exc:
            self._service.record_error("internal_error")
            raise MapsError("internal_error", log_detail=type(exc).__name__) from exc

    async def aclose(self) -> None:
        await self._service.google.aclose()

    async def __aenter__(self) -> PlacesClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
