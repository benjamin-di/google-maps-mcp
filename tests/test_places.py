"""Places: search and details shaped like Places API (New), from recorded responses."""

from __future__ import annotations

import pytest
from conftest import ALBERTINA, CAFE_CENTRAL

from maps_mcp.errors import MapsError

KHM = "ChIJ8XfPY5oHbUcRQbimbLVSYG8"


async def test_search_places_shape(service, transport):
    result = await service.search_places("museum", 48.2049, 16.3684, 2000, 2)
    first = result["places"][0]
    assert first["id"] == KHM
    assert first["displayName"] == {"text": "Kunsthistorisches Museum Wien", "languageCode": "en"}
    assert first["formattedAddress"] == "Maria-Theresien-Platz, 1010 Wien, Austria"  # name prefix stripped
    assert first["googleMapsUri"] == f"https://www.google.com/maps/place/?q=place_id:{KHM}"
    assert first["rating"] == 4.8 and first["userRatingCount"] == 36289
    assert first["timeZone"] == {"id": "Europe/Vienna"}
    assert set(first) >= {"location", "websiteUri", "priceLevel", "currentOpeningHours", "hoursStatusText"}
    hours = first["currentOpeningHours"]
    assert hours["openNow"] is True  # Wednesday 12:00, open 10-18
    assert hours["nextCloseTime"] == "2026-10-21T18:00:00+02:00"
    assert hours["weekdayDescriptions"][0].startswith("Monday: ")
    assert len(result["places"]) == 2
    # The location bias sets the viewport around the point.
    assert "!2d16.3684!3d48.2049" in transport.paths[0]


async def test_search_is_cached(service, transport):
    await service.search_places("museum")
    await service.search_places("MUSEUM ")
    assert len(transport.paths) == 1


async def test_single_exact_match_search(service):
    result = await service.search_places("Café Sperl Gumpendorfer Straße")
    assert [p["displayName"]["text"] for p in result["places"]] == ["Café Sperl"]


async def test_search_needs_both_coordinates(service):
    with pytest.raises(MapsError) as err:
        await service.search_places("cafe", latitude=48.2)
    assert err.value.code == "invalid_argument"


async def test_get_place_details(service, transport):
    place = await service.get_place(ALBERTINA)
    assert place["displayName"]["text"] == "Albertina"
    assert place["internationalPhoneNumber"] == "+43 1 534830"
    assert place["websiteUri"] == "https://www.example.com/"  # scrubbed in the recording
    assert place["primaryTypeDisplayName"]["text"] == "Art museum"
    hours = place["currentOpeningHours"]
    wednesday = [p for p in hours["periods"] if p["open"]["date"] == {"year": 2026, "month": 10, "day": 21}]
    assert [(p["open"]["hour"], p["close"]["hour"]) for p in wednesday] == [(10, 21)]
    assert wednesday[0]["open"]["day"] == 3  # Places API: 0 = Sunday
    assert hours["openNow"] is True and hours["nextCloseTime"] == "2026-10-21T21:00:00+02:00"
    assert transport.paths[0].startswith("/maps/preview/place?")
    await service.get_place(ALBERTINA)
    assert len(transport.paths) == 1  # cached


async def test_place_without_hours_has_null_hours(service):
    place = await service.get_place(CAFE_CENTRAL)
    assert place["displayName"]["text"] == "Café Central" and place["rating"] == 4.3
    assert place["currentOpeningHours"] is None


async def test_get_place_accepts_feature_ids(service):
    place = await service.get_place("0x476d079bf27e5ac1:0x66ef8a989f37deb8")
    assert place["id"] == ALBERTINA


@pytest.mark.parametrize("bad", ["Albertina", "GhIJQWDl0CIeQUARxks3icF8U8A", "0x1"])
async def test_get_place_rejects_other_ids(service, bad):
    with pytest.raises(MapsError) as err:
        await service.get_place(bad)
    assert err.value.code == "invalid_argument"


async def test_unknown_place(service):
    with pytest.raises(MapsError) as err:
        await service.get_place("ChIJAAAAAAAABDQRDFz86-Qpbto")  # valid shape, no such place here
    assert err.value.code == "place_not_found"
