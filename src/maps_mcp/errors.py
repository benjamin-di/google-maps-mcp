"""The error taxonomy. Tools report ``{code, next_step, retryable}`` and nothing else.

Exception text never reaches a tool result: every message a model sees comes from the fixed
``NEXT_STEP`` table below plus, for a few codes, our own detail text (which stop, which argument).
"""

from __future__ import annotations

from typing import Any

NEXT_STEP: dict[str, str] = {
    "invalid_argument": "Fix the arguments and call again.",
    "place_not_found": (
        "Google Maps has no place for this ID. Find the place again with maps_search_places and use the id it returns."
    ),
    "waypoint_not_found": (
        "Google Maps could not find this stop. Make it more specific (add the city or the street), "
        "or pass the place's id from maps_search_places or 'lat,lng'."
    ),
    "route_not_found": (
        "Google Maps found no route connecting these stops in this mode (an island, a border, or a "
        "country Google does not route in). Try another travelMode or other stops."
    ),
    "too_many_elements": "Ask for fewer origins x destinations (at most 25 pairs) or split the matrix.",
    "blocked": (
        "Google pushed back (rate limit, /sorry or captcha). No Google calls until the breaker closes "
        "(maps_status shows when). Do not retry now."
    ),
    "budget_exhausted": (
        "The daily Google request budget is spent. It resets at midnight (maps_status shows the day and zone)."
    ),
    "busy": "Other requests to Google are still running. Retry in a few seconds.",
    "upstream_error": "Google Maps or the network failed after retries. Retry in a minute.",
    "schema_changed": (
        "Google Maps' response format changed and this tool cannot read it; other tools may still work. "
        "Tell the person running this server: it needs an update."
    ),
    "internal_error": "maps-mcp failed unexpectedly. Retry once; if it repeats, tell the operator.",
}

RETRYABLE = frozenset({"busy", "upstream_error", "internal_error"})
# The caller's own mistakes: routine, logged at INFO.
AGENT_INPUT_CODES = frozenset(
    {"invalid_argument", "place_not_found", "waypoint_not_found", "route_not_found", "too_many_elements"}
)


class MapsError(Exception):
    """A classified failure. ``log_detail`` is for logs only."""

    def __init__(self, code: str, detail: str | None = None, *, log_detail: str | None = None) -> None:
        if code not in NEXT_STEP:
            raise ValueError(f"unknown error code {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.log_detail = log_detail

    def payload(self) -> dict[str, Any]:
        next_step = NEXT_STEP[self.code]
        if self.detail:
            next_step = f"{self.detail}. {next_step}"
        return {"code": self.code, "next_step": next_step, "retryable": self.code in RETRYABLE}


def invalid(detail: str) -> MapsError:
    return MapsError("invalid_argument", detail)
