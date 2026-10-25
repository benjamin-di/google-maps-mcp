"""Library results -> the field names and formats of Google's official APIs.

Places: Places API (New) ``Place``; routes: Routes API v2 ``Route``/``RouteLeg`` and the
``computeRouteMatrix`` element. Fields the official APIs do not have, but the Maps page gives us,
are added under their own names (``categories``, ``holidays``, ``hoursStatusText``,
``durationRange``, ``departureTime``/``arrivalTime``, ``waypoints``). Every key is present; null
means Google did not say. Durations are Google's ``"<seconds>s"`` strings, distances integers.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .vendor.gmaps.rpc.directions import (
    Directions,
    DirectionsWaypoint,
    Route,
    RouteLeg,
    RouteStep,
    TimePoint,
    encode_polyline,
)
from .vendor.gmaps.rpc.parser import ParsedPlace

WEEK_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PRICE_LEVELS = {
    1: "PRICE_LEVEL_INEXPENSIVE",
    2: "PRICE_LEVEL_MODERATE",
    3: "PRICE_LEVEL_EXPENSIVE",
    4: "PRICE_LEVEL_VERY_EXPENSIVE",
}
TRAVEL_MODES = {"DRIVE": "drive", "WALK": "walk", "BICYCLE": "bicycle"}
URL_TRAVEL_MODES = {"drive": "driving", "walk": "walking", "bicycle": "bicycling"}


def place_uri(place_id: str | None) -> str | None:
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else None


def _formatted_address(place: ParsedPlace) -> str | None:
    # The search payload's full address leads with the name ("Albertina, Albertinapl. 1, ...").
    address = place.address or ""
    prefix = f"{place.name}, "
    if place.name and address.startswith(prefix):
        address = address[len(prefix) :]
    return address or None


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _point(moment: dt.datetime) -> dict[str, Any]:
    return {
        # Places API: 0 = Sunday ... 6 = Saturday.
        "day": (moment.isoweekday()) % 7,
        "hour": moment.hour,
        "minute": moment.minute,
        "date": {"year": moment.year, "month": moment.month, "day": moment.day},
    }


def _intervals(days: list[dict[str, Any]], zone: ZoneInfo | None) -> list[tuple[dt.datetime, dt.datetime]]:
    """Absolute opening intervals for the dated days (naive if no zone is known)."""
    out: list[tuple[dt.datetime, dt.datetime]] = []
    for day in days:
        try:
            date = dt.date.fromisoformat(day["date"])
        except (KeyError, TypeError, ValueError):
            continue
        for period in day.get("periods") or []:
            try:
                open_h, open_m = (int(x) for x in str(period["open"]).split(":"))
                close_h, close_m = (int(x) for x in str(period["close"]).split(":"))
            except (KeyError, ValueError):
                continue
            start = dt.datetime.combine(date, dt.time(open_h, open_m), zone)
            end_date = date + dt.timedelta(days=1) if close_h == 24 or period.get("closes_next_day") else date
            end = dt.datetime.combine(end_date, dt.time(0 if close_h == 24 else close_h, close_m), zone)
            out.append((start, end))
    return sorted(out)


def _merge(intervals: list[tuple[dt.datetime, dt.datetime]]) -> list[tuple[dt.datetime, dt.datetime]]:
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def opening_hours(place: ParsedPlace, now: dt.datetime) -> dict[str, Any] | None:
    """``currentOpeningHours`` for the next seven days, as the Places API shapes it."""
    days = list(place.opening_hours or [])
    if not days:
        return None
    zone = _zone(place.timezone)
    periods = []
    for start, end in _intervals(days, zone):
        periods.append({"open": _point(start), "close": _point(end)})
    by_weekday = {d.get("weekday"): d for d in days}
    weekday_descriptions = [
        f"{name}: {', '.join(by_weekday[name].get('text') or []) or 'Closed'}"
        for name in WEEK_ORDER
        if name in by_weekday
    ]
    special = [d for d in days if d.get("special")]
    result: dict[str, Any] = {
        "openNow": None,
        "periods": periods,
        "weekdayDescriptions": weekday_descriptions,
        "specialDays": [{"date": _date_obj(d["date"])} for d in special],
        "nextOpenTime": None,
        "nextCloseTime": None,
        # Not in the Places API: the holiday Google attaches to a date, and whether the place
        # confirmed its hours that day (false: Maps says the holiday "might affect these hours").
        "holidays": [
            {
                "date": d["date"],
                "name": d["special"].get("name") or None,
                "confirmed": bool(d["special"].get("confirmed")),
                "hours": d.get("text") or [],
            }
            for d in special
        ],
    }
    if zone is None:
        return result
    local_now = now.astimezone(zone)
    intervals = _intervals(days, zone)
    # The list starts today: yesterday's after-midnight closing comes from the same weekday a week on.
    last = days[-1]
    if any(p.get("closes_next_day") for p in last.get("periods") or []) and not last.get("special"):
        intervals += [(s - dt.timedelta(days=7), e - dt.timedelta(days=7)) for s, e in _intervals([last], zone)]
    merged = _merge(sorted(intervals))
    current = next(((s, e) for s, e in merged if s <= local_now < e), None)
    result["openNow"] = current is not None
    if current is not None:
        # Open round the clock for the whole week Google gave: no closing time to name.
        always = current[1] - current[0] >= dt.timedelta(days=6)
        result["nextCloseTime"] = None if always else current[1].isoformat()
    else:
        upcoming = next((s for s, _ in merged if s > local_now), None)
        result["nextOpenTime"] = upcoming.isoformat() if upcoming else None
    return result


def visit_status(place: ParsedPlace, arrive: dt.datetime, leave: dt.datetime) -> dict[str, Any]:
    """Whether a place is open for a visit from ``arrive`` to ``leave`` (aware datetimes):
    state open | closes_during_visit | closed | unknown, with the hours text for that day."""
    zone = _zone(place.timezone)
    days = list(place.opening_hours or [])
    if zone is None or not days:
        return {"state": "unknown", "text": None, "hours": None}
    local_arrive, local_leave = arrive.astimezone(zone), leave.astimezone(zone)
    day = next((d for d in days if d.get("date") == local_arrive.date().isoformat()), None)
    hours = ", ".join(day.get("text") or []) if day else None
    intervals = _intervals(days, zone)
    last = days[-1]
    if any(p.get("closes_next_day") for p in last.get("periods") or []) and not last.get("special"):
        intervals += [(s - dt.timedelta(days=7), e - dt.timedelta(days=7)) for s, e in _intervals([last], zone)]
    merged = _merge(sorted(intervals))
    if day is None and not any(s <= local_arrive < e for s, e in merged):
        return {"state": "unknown", "text": "Google lists no hours for that date", "hours": None}
    current = next(((s, e) for s, e in merged if s <= local_arrive < e), None)
    if current is None:
        upcoming = next((s for s, _ in merged if s > local_arrive), None)
        text = f"Closed on arrival; opens {upcoming.strftime('%a %H:%M')}" if upcoming else "Closed on arrival"
        return {"state": "closed", "text": text, "hours": hours}
    if current[1] < local_leave:
        return {"state": "closes_during_visit", "text": f"Closes {current[1].strftime('%H:%M')}", "hours": hours}
    if current[1] - current[0] >= dt.timedelta(hours=24):
        text = "Open 24 hours"
    elif current[1].date() > local_arrive.date():
        text = f"Open until {current[1].strftime('%H:%M')} (next day)"
    else:
        text = f"Open until {current[1].strftime('%H:%M')}"
    return {"state": "open", "text": text, "hours": hours}


def _date_obj(value: str) -> dict[str, int]:
    date = dt.date.fromisoformat(value)
    return {"year": date.year, "month": date.month, "day": date.day}


def place(place: ParsedPlace, now: dt.datetime, language: str = "en") -> dict[str, Any]:
    phone = place.phone or None
    return {
        "id": place.place_id or None,
        "displayName": {"text": place.name, "languageCode": language} if place.name else None,
        "formattedAddress": _formatted_address(place),
        "location": (
            {"latitude": place.latitude, "longitude": place.longitude}
            if place.latitude is not None and place.longitude is not None
            else None
        ),
        "googleMapsUri": place_uri(place.place_id),
        "primaryTypeDisplayName": (
            {"text": place.categories[0], "languageCode": language} if place.categories else None
        ),
        "categories": list(place.categories) or None,
        "rating": place.rating,
        "userRatingCount": place.review_count if place.rating is not None else None,
        "internationalPhoneNumber": phone if phone and phone.startswith("+") else None,
        "nationalPhoneNumber": phone if phone and not phone.startswith("+") else None,
        "websiteUri": place.website or None,
        "priceLevel": PRICE_LEVELS.get(place.price_level) if place.price_level else None,
        "priceRangeText": place.price_range or None,
        "timeZone": {"id": place.timezone} if place.timezone else None,
        "currentOpeningHours": opening_hours(place, now),
        "hoursStatusText": place.status or None,
    }


# -- routes ---------------------------------------------------------------------------------


def seconds(value: int | None) -> str | None:
    return f"{value}s" if value is not None else None


def _text(value: str) -> dict[str, str] | None:
    return {"text": value} if value else None


def _time(point: TimePoint | None) -> str | None:
    if point is None:
        return None
    zone: dt.tzinfo = _zone(point.timezone) or dt.UTC
    if point.utc_offset_seconds is not None and _zone(point.timezone) is None:
        zone = dt.timezone(dt.timedelta(seconds=point.utc_offset_seconds))
    return dt.datetime.fromtimestamp(point.epoch, zone).isoformat()


def _lat_lng(waypoint: DirectionsWaypoint) -> dict[str, Any] | None:
    if not waypoint.resolved:
        return None
    return {"latLng": {"latitude": waypoint.latitude, "longitude": waypoint.longitude}}


def waypoint(w: DirectionsWaypoint) -> dict[str, Any]:
    return {
        "query": w.query,
        "displayName": w.name or None,
        "formattedAddress": w.address or None,
        "placeId": w.place_id or None,
        "location": {"latitude": w.latitude, "longitude": w.longitude} if w.resolved else None,
    }


def step(s: RouteStep) -> dict[str, Any]:
    """A turn, as Routes API ``RouteLegStep`` (``notices`` is ours)."""
    start = (
        {"latLng": {"latitude": s.start_latitude, "longitude": s.start_longitude}}
        if s.start_latitude is not None and s.start_longitude is not None
        else None
    )
    return {
        "distanceMeters": s.distance_meters,
        "staticDuration": seconds(s.duration_seconds),
        "startLocation": start,
        "navigationInstruction": {"maneuver": s.maneuver, "instructions": s.instruction or None},
        "localizedValues": {"distance": _text(s.distance_text), "staticDuration": _text(s.duration_text)},
        "notices": list(s.notices),
    }


def _segments(leg: RouteLeg) -> list[dict[str, Any]]:
    """The grouped lines Maps lists above the turns, pointing into the leg's steps."""
    out, first = [], 0
    for segment in leg.segments:
        out.append(
            {
                "instructions": segment.instruction or None,
                "distanceMeters": segment.distance_meters,
                "staticDuration": seconds(segment.duration_seconds),
                "firstStepIndex": first,
                "stepCount": len(segment.steps),
            }
        )
        first += len(segment.steps)
    return out


def route(
    r: Route, waypoints: list[DirectionsWaypoint], *, include_steps: bool = False, include_polyline: bool = False
) -> dict[str, Any]:
    legs = []
    for index, leg in enumerate(r.legs):
        start = waypoints[index] if index < len(waypoints) else None
        end = waypoints[index + 1] if index + 1 < len(waypoints) else None
        legs.append(
            {
                "distanceMeters": leg.distance_meters,
                # Google gives legs their typical duration only (no traffic split).
                "duration": seconds(leg.duration_seconds),
                "staticDuration": seconds(leg.duration_seconds),
                "startLocation": _lat_lng(start) if start else None,
                "endLocation": _lat_lng(end) if end else None,
                "localizedValues": {
                    "distance": _text(leg.distance_text),
                    "duration": _text(leg.duration_text),
                    "staticDuration": _text(leg.duration_text),
                },
                "steps": [step(s) for seg in leg.segments for s in seg.steps] if include_steps else None,
                "segments": _segments(leg) if include_steps else None,
            }
        )
    range_ = r.duration_range_seconds
    return {
        "description": r.summary or None,
        "distanceMeters": r.distance_meters,
        "duration": seconds(r.duration_seconds),
        "staticDuration": seconds(r.static_duration_seconds),
        "localizedValues": {
            "distance": _text(r.distance_text),
            "duration": _text(r.duration_text),
            "staticDuration": _text(r.static_duration_text),
        },
        "durationWithoutTraffic": seconds(r.duration_without_traffic_seconds),
        "durationRange": (
            {"min": seconds(range_[0]), "max": seconds(range_[1]), "text": r.duration_range_text or None}
            if range_
            else None
        ),
        "departureTime": _time(r.departure),
        "arrivalTime": _time(r.arrival),
        "legs": legs,
        "polyline": {"encodedPolyline": encode_polyline(r.path)} if include_polyline and r.path else None,
    }


def _url_stop(w: DirectionsWaypoint) -> str:
    if w.name or w.address:
        return ", ".join(x for x in (w.name, w.address) if x)
    if w.resolved:
        return f"{w.latitude},{w.longitude}"
    return w.query


def directions_uri(waypoints: list[DirectionsWaypoint], mode: str) -> str | None:
    """A Maps URLs link (keyless) that opens the same route; at most 9 intermediate stops."""
    if len(waypoints) < 2 or len(waypoints) > 11 or mode not in URL_TRAVEL_MODES:
        return None
    origin, *middle, destination = waypoints
    params: dict[str, str] = {"api": "1", "origin": _url_stop(origin), "destination": _url_stop(destination)}
    if origin.place_id:
        params["origin_place_id"] = origin.place_id
    if destination.place_id:
        params["destination_place_id"] = destination.place_id
    if middle:
        params["waypoints"] = "|".join(_url_stop(w) for w in middle)
        if all(w.place_id for w in middle):
            params["waypoint_place_ids"] = "|".join(w.place_id for w in middle)
    params["travelmode"] = URL_TRAVEL_MODES[mode]
    return "https://www.google.com/maps/dir/?" + urlencode(params, safe="|,")


def directions(
    result: Directions,
    mode: str,
    *,
    alternatives: bool,
    include_steps: bool = False,
    include_polyline: bool = False,
) -> dict[str, Any]:
    routes = result.routes if alternatives else result.routes[:1]
    return {
        "travelMode": next(k for k, v in TRAVEL_MODES.items() if v == mode),
        "routes": [
            route(r, result.waypoints, include_steps=include_steps, include_polyline=include_polyline) for r in routes
        ],
        "waypoints": [waypoint(w) for w in result.waypoints],
        "googleMapsUri": directions_uri(result.waypoints, mode),
    }
