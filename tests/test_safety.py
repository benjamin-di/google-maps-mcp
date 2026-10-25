"""Static guards over the shipped source. A new host, network import or upstream feature in use
must be a deliberate change here."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "maps_mcp"
OURS = sorted(p for p in SRC.rglob("*.py") if "vendor" not in p.parts)
VENDORED = sorted((SRC / "vendor").rglob("*.py"))
# Hosts any request may go to: Maps itself, and the consent pages the first requests visit.
GOOGLE_HOSTS = {"www.google.com", "google.com", "maps.google.com", "consent.google.com"}
VENDOR = SRC / "vendor" / "gmaps"
NETWORK_MODULES = {"httpx", "requests", "urllib3", "aiohttp", "socket", "http", "curl_cffi"}


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_google_py_imports_a_network_library():
    for path in OURS:
        found = _imports(path) & NETWORK_MODULES
        if path.name == "google.py":
            assert found <= {"httpx"}, found  # exception types, and the map-tile client
        else:
            assert not found, (path, found)


PHOTO_HOSTS = {f"lh{n}.googleusercontent.com" for n in (3, 4, 5, 6)}


def test_only_google_maps_hosts_in_url_literals():
    hosts = set()
    for path in OURS + VENDORED:
        found = set(re.findall(r"https?://([^/\"'\s`]+)", path.read_text()))
        if path.name == "server.py":
            found -= PHOTO_HOSTS  # the card view's CSP: the person's browser loads photos, not this server
        hosts |= found
    assert hosts <= GOOGLE_HOSTS, hosts


def test_destination_allowlist_stays_explicit():
    from maps_mcp.google import ALLOWED_GOOGLE_HOSTS

    assert ALLOWED_GOOGLE_HOSTS == GOOGLE_HOSTS


def test_card_view_loads_only_google_tiles_and_photos_and_connects_nowhere():
    from maps_mcp import server

    html = (SRC / "view.html").read_text()
    assert set(server.VIEW_CSP) == {"resourceDomains"}
    assert set(server.VIEW_CSP["resourceDomains"]) == {"https://www.google.com", *(f"https://{h}" for h in PHOTO_HOSTS)}
    assert set(re.findall(r"https://([a-z0-9.-]+)", html)) <= {"www.google.com", *PHOTO_HOSTS}
    sinks = (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc", "fetch(", "XMLHttpRequest",
        "WebSocket", "EventSource", "sendBeacon", "eval(", "Function(", "javascript:", "importScripts",
    )  # fmt: skip
    for sink in sinks:
        assert sink not in html, sink


def test_we_use_only_search_places_and_directions_from_the_library():
    google = (SRC / "google.py").read_text()
    used = set(re.findall(r"client\.(\w+)\.(\w+)\(", google))
    assert used == {("search", "places"), ("places", "get"), ("directions", "route")}
    for path in OURS:
        text = path.read_text()
        for feature in (
            "extract_contacts",
            ".reviews.",
            "WebsiteContactExtractor",
            "enrich(",
            "login_cookies",
            "identity=",
        ):
            assert feature not in text or path.name == "test_safety.py", (path, feature)


def test_vendored_library_is_pinned_and_complete():
    commit = (VENDOR / "VENDORED_COMMIT").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert (VENDOR / "LICENSE").read_text().startswith("MIT License")
    assert not (VENDOR / "cli.py").exists() and not (VENDOR / "mcp_server.py").exists()
    for needed in ("_directions.py", "rpc/directions.py", "rpc/ids.py", "_places.py", "_search.py", "_hooks.py"):
        assert (VENDOR / needed).exists(), needed


FORBIDDEN_IN_FIXTURES = re.compile(
    r"aclk|adview|aboutthisad|sxsrf|gclid|maps/contrib|AOvVaw|[02]ahUKE|HK\$|HKD|\+852|[A-Z]{1,3}\$\s?\d",
    re.IGNORECASE,
)


def test_fixtures_hold_no_ads_reviewers_tokens_or_requester_traces():
    fixtures = Path(__file__).parent / "fixtures"
    for path in sorted(fixtures.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert not FORBIDDEN_IN_FIXTURES.search(text), (path.name, FORBIDDEN_IN_FIXTURES.search(text).group(0))
        # Every URL is Google's or the scrub placeholder.
        for host in set(re.findall(r"https?://([^/\"'\s]+)", text)):
            assert host in {"www.google.com", "search.google.com", "lh3.googleusercontent.com", "www.example.com"}, (
                path.name,
                host,
            )


def test_tool_errors_carry_no_exception_text():
    from maps_mcp.errors import NEXT_STEP, MapsError

    for code in NEXT_STEP:
        payload = MapsError(code).payload()
        assert set(payload) == {"code", "next_step", "retryable"} and payload["next_step"] == NEXT_STEP[code]
