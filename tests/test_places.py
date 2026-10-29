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


BOOKING = [
    # [url, source domain, ...] as Google lists "Reserve a table" (synthetic, example.com hosts).
    ["https://www.example.com/r/central?src=ggl&utm_source=google&rwg_token=AJKvS9x%3D%3D", "example.com", None],
    ["https://book.example.com/v/42?ref=7", "book.example.com"],
    ["javascript:alert(1)", "evil.example"],
]


def with_links(name: str) -> object:
    from conftest import load

    raw = load(name)
    raw[6][46] = BOOKING
    raw[6][38] = ["https://www.example.com/menu?utm_medium=maps", "example.com"]
    return raw


async def test_place_reservation_links_and_menu(service, transport):
    from conftest import default_responder

    transport.responder = lambda path: (
        with_links("details_cafe.json") if "0x476d0797f70c3b2f" in path else default_responder(path)
    )
    place = await service.get_place(CAFE_CENTRAL)
    assert place["reservationLinks"] == [
        # Google's attribution parameters are dropped, the site's own (src, ref) kept.
        {"uri": "https://www.example.com/r/central?src=ggl", "provider": "example.com"},
        {"uri": "https://book.example.com/v/42?ref=7", "provider": "book.example.com"},
    ]
    assert place["menuUri"] == "https://www.example.com/menu"


async def test_places_without_links(service):
    place = await service.get_place(ALBERTINA)
    assert place["reservationLinks"] == [] and place["menuUri"] is None
    found = await service.search_places("museum")
    assert all(p["reservationLinks"] == [] and p["menuUri"] is None for p in found["places"])


async def test_search_results_carry_reservation_links(service, transport):
    from conftest import default_responder, load

    from maps_mcp.vendor.gmaps.rpc.parser import _search_place_data_entries

    raw = load("search_single.json")
    for _, place_data in _search_place_data_entries(raw):
        place_data[46] = BOOKING[:1]
    transport.responder = lambda path: raw if path.startswith("/search") else default_responder(path)
    found = await service.search_places("Café Sperl Gumpendorfer Straße")
    assert found["places"][0]["reservationLinks"] == [
        {"uri": "https://www.example.com/r/central?src=ggl", "provider": "example.com"}
    ]
    assert len(transport.paths) == 1  # no details request


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://a.example.com/x?utm_source=g&UTM_campaign=y", "https://a.example.com/x"),
        ("https://a.example.com/x?q=a%26b&gclid=1#frag", "https://a.example.com/x?q=a%26b#frag"),
        ("http://a.example.com/", "http://a.example.com/"),
        ("ftp://a.example.com/", None),
        ("/relative", None),
        ("", None),
        (None, None),
    ],
)
def test_link_uri(given, expected):
    from maps_mcp.shapes import link_uri

    assert link_uri(given) == expected
