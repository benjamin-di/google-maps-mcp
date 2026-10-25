"""currentOpeningHours and visit status from real dated [203] blocks (Vienna, recorded 2026-10-21).

SPAR Gourmet is confirmed closed on Austrian National Day (Mon 26 Oct); BILLA only carries Google's
"might affect these hours" notice; the bp station is open 24 hours; Loos American Bar closes at 04:00
the next day; Steirereck has lunch and dinner sittings, the second until midnight. Vienna leaves
summer time on Sunday 25 Oct (+02:00 -> +01:00).
"""

from __future__ import annotations

import datetime as dt

from conftest import load

from maps_mcp import shapes
from maps_mcp.vendor.gmaps.rpc.parser import ParsedPlace, _parse_opening_hours

SAMPLES = load("hours_samples.json")


def _place(name: str) -> ParsedPlace:
    sample = SAMPLES[name]
    return ParsedPlace(name=name, timezone=sample["timezone"], opening_hours=_parse_opening_hours(sample["hours_203"]))


def _at(name: str, iso: str) -> dict:
    return shapes.opening_hours(_place(name), dt.datetime.fromisoformat(iso))


def test_confirmed_holiday_closure_shows_in_periods_and_holidays():
    hours = _at("SPAR Gourmet", "2026-10-26T10:00:00+01:00")
    assert hours["openNow"] is False
    assert hours["nextOpenTime"] == "2026-10-27T07:30:00+01:00"
    assert hours["specialDays"] == [{"date": {"year": 2026, "month": 10, "day": 26}}]
    assert hours["holidays"] == [
        {"date": "2026-10-26", "name": "Austrian National Day", "confirmed": True, "hours": ["Closed"]}
    ]
    assert not any(p["open"]["date"]["day"] == 26 for p in hours["periods"])


def test_unconfirmed_holiday_keeps_regular_hours():
    hours = _at("BILLA", "2026-10-26T10:00:00+01:00")
    assert hours["openNow"] is True
    assert hours["holidays"][0]["confirmed"] is False


def test_dst_end_is_handled_by_the_zone():
    hours = _at("SPAR Gourmet", "2026-10-21T20:30:00+02:00")  # closed at 20:00 on Wednesday
    assert hours["openNow"] is False
    assert hours["nextOpenTime"] == "2026-10-22T07:30:00+02:00"
    monday = _at("SPAR Gourmet", "2026-10-27T06:00:00+01:00")  # after the switch
    assert monday["nextOpenTime"] == "2026-10-27T07:30:00+01:00"


def test_open_24_hours_has_no_close_within_the_week():
    hours = _at("bp", "2026-10-21T03:00:00+02:00")
    assert hours["openNow"] is True and hours["nextCloseTime"] is None


def test_after_midnight_close_counts_for_the_early_hours_of_today():
    # 02:00 Wednesday: Tuesday's 12:00-04:00 (taken from next Tuesday's entry) is still open.
    hours = _at("Loos American Bar", "2026-10-21T02:00:00+02:00")
    assert hours["openNow"] is True
    assert hours["nextCloseTime"] == "2026-10-21T04:00:00+02:00"
    close = hours["periods"][0]["close"]
    assert (close["hour"], close["date"]["day"]) == (4, 22)  # Wednesday's closes Thursday 04:00


def test_midnight_close_and_split_sittings():
    hours = _at("Steirereck", "2026-10-21T23:30:00+02:00")
    assert hours["openNow"] is True
    assert hours["nextCloseTime"] == "2026-10-22T00:00:00+02:00"
    between = _at("Steirereck", "2026-10-21T16:00:00+02:00")
    assert between["openNow"] is False and between["nextOpenTime"] == "2026-10-21T18:30:00+02:00"


def test_last_day_of_the_window_still_names_its_closing_time():
    hours = _at("SPAR Gourmet", "2026-10-27T12:00:00+01:00")  # the last dated day Google sent
    assert hours["openNow"] is True and hours["nextCloseTime"] == "2026-10-27T20:00:00+01:00"


def test_weekday_descriptions_run_monday_to_sunday():
    hours = _at("Steirereck", "2026-10-21T12:00:00+02:00")
    assert [d.split(":")[0] for d in hours["weekdayDescriptions"]] == shapes.WEEK_ORDER


def test_visit_status():
    at = dt.datetime.fromisoformat
    assert (
        shapes.visit_status(_place("bp"), at("2026-10-27T03:00:00+01:00"), at("2026-10-27T03:30:00+01:00"))["text"]
        == "Open 24 hours"
    )
    bar = shapes.visit_status(
        _place("Loos American Bar"), at("2026-10-21T22:00:00+02:00"), at("2026-10-21T23:00:00+02:00")
    )
    assert bar == {"state": "open", "text": "Open until 04:00 (next day)", "hours": "12 PM–4 AM"}
    late = shapes.visit_status(_place("Steirereck"), at("2026-10-21T13:30:00+02:00"), at("2026-10-21T15:00:00+02:00"))
    assert late["state"] == "closes_during_visit" and late["text"] == "Closes 14:30"
    closed = shapes.visit_status(
        _place("SPAR Gourmet"), at("2026-10-26T10:00:00+01:00"), at("2026-10-26T10:30:00+01:00")
    )
    assert closed["state"] == "closed" and closed["text"] == "Closed on arrival; opens Tue 07:30"


def test_no_hours_is_null_and_unknown_zone_skips_now():
    assert shapes.opening_hours(ParsedPlace(name="x"), dt.datetime.now(dt.UTC)) is None
    place = _place("SPAR Gourmet")
    place.timezone = ""
    hours = shapes.opening_hours(place, dt.datetime.now(dt.UTC))
    assert hours["openNow"] is None and hours["periods"]
    now = dt.datetime(2026, 10, 21, 12, tzinfo=dt.UTC)
    assert shapes.visit_status(ParsedPlace(name="x"), now, now)["state"] == "unknown"
