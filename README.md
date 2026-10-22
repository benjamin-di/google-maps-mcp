# google-maps-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that gives AI assistants Google Maps:
**how long it takes to get from A to B** (driving with traffic, walking, cycling), **turn-by-turn
directions**, **multi-stop route planning**, **places with their opening hours**, and **map
images**, without an API key, billing or a Google account. It reads the same data the
google.com/maps web page loads when you are signed out.

Results use the field names of Google's official
[Places API (New)](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places)
and [Routes API](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes),
so they read like those APIs' JSON. In clients that support MCP Apps (claude.ai, ChatGPT),
routes and places are drawn as interactive cards.

> Unofficial. Not affiliated with, endorsed or sponsored by Google. Automated access to
> google.com/maps is not covered by Google's terms of service: use it for personal,
> low-volume purposes, at your own risk (see [Terms and limits](#terms-and-limits)).

## Tools

| Tool | What it returns | Google requests |
|---|---|---|
| `maps_search_places(textQuery, latitude, longitude, radiusMeters, pageSize)` | Places like `places:searchText`: id, name, address, location, rating, review count, category, phone, website, price, time zone, `currentOpeningHours` | 1 |
| `maps_get_place(id)` | One place by its id (`ChIJ…`), the same fields | 1 |
| `maps_compute_route(origin, destination, intermediates≤8, travelMode, departureTime \| arrivalTime, computeAlternativeRoutes, includeSteps, includePolyline)` | Like `computeRoutes`: distance and duration per route and leg; for driving, duration in traffic (now or at the given time), typical duration, Google's range ("26–50 min") and the arrival or departure time; optional turn-by-turn steps and route line; a Google Maps link | 1 |
| `maps_compute_route_matrix(origins, destinations, travelMode, departureTime)` | Like `computeRouteMatrix`, up to 25 pairs | 1 per pair |
| `maps_plan_route(origin, stops≤8, destination \| roundTrip, travelMode, departureTime, dwellMinutes, optimizeOrder)` | A multi-stop plan: the fastest order, arrival and leave time per stop, whether each stop is open on arrival, warnings | 1 + up to 5 candidate orders + 1 per stop for hours |
| `maps_show_route(...)` | The same plan as an interactive card | as `maps_plan_route` |
| `maps_show_places(items[{id, note}]≤10, title)` | Place cards with photo, rating, open now, today's hours, holidays, links, and a pin map | 1 per place |
| `maps_static_map(center, zoom, size, maptype, markers, route)` | A JPEG like the Static Maps API: an area, lettered pins and/or a route line; roadmap, satellite, hybrid or terrain | the lookups, plus about 10-25 map tiles |
| `maps_status(check)` | Daily budgets, breaker, last error; `check=true` runs one real check (cached 55 min) | 0 (or 3) |

Durations are Google's `"<seconds>s"` strings and distances are meters. Every key is present;
`null` means Google did not say. Stops are a name or address, a place id from
`maps_search_places`, or `"lat,lng"`; times are the local wall-clock time at the stop
(`2026-10-29T08:30`).

**Opening hours.** `currentOpeningHours` covers the next seven days with dates, so holiday
hours land on the right day: `periods`, `weekdayDescriptions`, `openNow`,
`nextOpenTime`/`nextCloseTime` (in the place's time zone), and `holidays`: the holiday Google
attaches to a date, with `confirmed: false` when Maps only says it "might affect these hours".

**Turn-by-turn.** `includeSteps` adds Routes API `steps` per leg (`navigationInstruction`
{`maneuver`, `instructions`} such as "Turn left onto Gertrude-Fröhlich-Sandner-Straße",
distance, time, start point, plus `notices` like "Toll road") and the grouped `segments` Maps
lists ("Get on A4"). `maneuver` is Google's own code where the step carries one (ON_RAMP,
MERGE, ROUNDABOUT_…, TURN_SLIGHT_RIGHT); it is never guessed from the text.

**Multi-stop planning.** Google's web directions cannot reorder waypoints and a full
travel-time matrix costs a request per pair, so `maps_plan_route` ranks every order of the
stops by straight-line length, routes the four shortest plus the order given with real Google
directions, and keeps the fastest (`savedVsGivenOrder` says what that saved). Stays
(`dwellMinutes`) and traffic shift the arrival times; each stop's `openingStatus` is `open`,
`closes_during_visit`, `closed` or `unknown`.

**Cards.** `maps_show_route` and `maps_show_places` return their facts to the model, and MCP
Apps hosts draw them with `ui://maps/view.html` (`src/maps_mcp/view.html`): a map built from
Google Maps tiles in your browser (drag, zoom, map/satellite, fit) with the route and lettered
pins, a stop timeline with arrival times and open/closed status, steps per leg and "Open in
Google Maps"; or place cards with photo, rating, today's hours and links. The route line,
steps and photos travel in the result's `_meta`, so they do not fill the model's context. The
view loads only from www.google.com and lh3-6.googleusercontent.com, connects nowhere, sets
every text as text and opens links through the host.

## Quick start

### Docker (HTTP)

```bash
docker run -d --name maps-mcp -p 127.0.0.1:8000:8000 \
  -e MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
  --read-only --tmpfs /state:uid=1000 \
  ghcr.io/benjamin-di/google-maps-mcp:latest
```

The MCP endpoint is `http://127.0.0.1:8000/mcp` (stateless streamable HTTP with JSON replies)
with `Authorization: Bearer <token>`; `/health` is open. `MCP_BEARER_TOKEN` may hold several
tokens separated by commas. For claude.ai or ChatGPT, put it behind HTTPS with a reverse proxy
they can reach. Mount a volume on `/state` to keep the daily budgets and breaker across
restarts.

### stdio (Claude Desktop, Claude Code, Codex)

```bash
# from PyPI, with uv (https://docs.astral.sh/uv/)
claude mcp add maps -- uvx google-maps-keyless-mcp --transport stdio
# from a checkout
claude mcp add maps -- uv --directory /path/to/google-maps-mcp run maps-mcp --transport stdio
# or with Docker
claude mcp add maps -- docker run -i --rm --tmpfs /state:uid=1000 ghcr.io/benjamin-di/google-maps-mcp:latest --transport stdio
```

Outside Docker, state goes to `$XDG_STATE_HOME/google-maps-mcp` (else
`~/.local/state/google-maps-mcp`); set `MAPS_STATE_DIR` to put it elsewhere.

### Build the image yourself

The image is two stages: the pinned dependencies (`Dockerfile.deps`) and the source on top.

```bash
docker build -f Dockerfile.deps -t maps-mcp-deps .
docker build -t google-maps-mcp .
```

### Command line

`maps-mcp [--transport http|stdio] [--host 127.0.0.1] [--port 8000]`: HTTP (the default)
listens on 127.0.0.1:8000 (or `$MAPS_HOST`) and requires `MCP_BEARER_TOKEN`; the image sets
`MAPS_HOST=0.0.0.0`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_BEARER_TOKEN` | (required for HTTP) | Accepted bearer token(s), comma-separated, each at least 32 characters |
| `MAPS_HOST` | `127.0.0.1` (image: `0.0.0.0`) | HTTP listen address when `--host` is not given |
| `MAPS_STATE_DIR` | per-user state dir (image: `/state`) | Budgets, breaker, Google's consent cookies |
| `MAPS_DAILY_REQUEST_BUDGET` | `1000` | Requests to google.com per day, all tools together |
| `MAPS_DAILY_TILE_BUDGET` | `3000` | Map tiles per day for `maps_static_map` |
| `MAPS_BUDGET_TZ` | `UTC` | The time zone whose midnight resets the budgets |
| `MAPS_MIN_INTERVAL_S` | `1.0` | Minimum seconds between requests (jittered ±30%) |
| `MAPS_REQUEST_TIMEOUT_S` | `15` | Per-request timeout |
| `MAPS_LANGUAGE` | `en` | Language of names and instructions |
| `MAPS_CANARY_FROM` / `MAPS_CANARY_TO` | Wien Hauptbahnhof / Albertina | The status check's walk; `MAPS_CANARY_TO` must be a place id with opening hours |
| `LOG_LEVEL` | `INFO` | JSON log lines on stderr; tool arguments are never logged, only short hashes |

## How it works

Four endpoints on www.google.com, the ones google.com/maps itself calls: `/search?tbm=map`
(search), `/maps/preview/place` (place details), `/maps/preview/directions` (routes, steps
and geometry) and `/maps/vt` (map tiles). Requests carry no account and no key; Google's
consent cookies are fetched once and kept in the state directory. Parsing comes from
[gmaps-scraper](https://github.com/nick-choudhary/gmaps-scraper), vendored in
`src/maps_mcp/vendor/gmaps` from a fork that adds this project's contributions, each offered
upstream as a pull request (see NOTICE). Refresh it with `scripts/vendor-gmaps.sh`; never edit
the vendored copy by hand.

It is a deliberately small, polite client: one request at a time, about one a second, daily
budgets, results cached in memory (places 6 h, walking and cycling routes 12 h, driving
10 min, or 1 h for a given time), and a breaker that stops all requests for 30 min, then 2 h,
then 8 h when Google answers 429, `/sorry` or a sign-in wall.

## Terms and limits

- Google's terms of service do not allow automated access to google.com/maps. This project is
  for personal, low-volume use; you are responsible for how you use it. If you need
  guarantees, use Google's paid [Maps Platform APIs](https://developers.google.com/maps).
- Do not store or redistribute Google's place data, and keep Google's attribution ("Map data
  © Google") with maps, as the cards and images do.
- Google can change its page format at any time. Parsing failures surface as
  `schema_changed`, never as wrong numbers; please open an issue.
- No walking or driving routes in South Korea (Google's own limit: `condition:
  ROUTE_NOT_FOUND` with a note). Data in mainland China is thin. Opening hours are Google's: a
  good secondary source, not proof.

| Error code | Meaning |
|---|---|
| `blocked` | Google pushed back; the breaker is open (`maps_status` says until when) |
| `schema_changed` | Google's response format changed; the tool cannot read it |
| `upstream_error` | Network or Google failed after retries |
| `waypoint_not_found` / `place_not_found` / `route_not_found` | The stop, the place id or a route could not be found; the message says which |
| `budget_exhausted`, `busy`, `too_many_elements`, `invalid_argument`, `internal_error` | as named |

## Development

```bash
make bootstrap   # uv sync
make check       # ruff, format, lock consistency, tests
```

Tests never reach Google: the vendored library runs against responses recorded from
google.com/maps (`tests/fixtures`, recorded and scrubbed by `scripts/capture-fixtures`; see
`tests/fixtures/README.md`). `tests/test_safety.py` guards the boundary: the only host is
www.google.com, and the card view loads only from its declared hosts and connects nowhere.

Contributions: see `CONTRIBUTING.md`; security problems: `SECURITY.md`.

## Credits

Built on [gmaps-scraper](https://github.com/nick-choudhary/gmaps-scraper) (MIT). The directions
request layout was first documented by
[ai-ecoverse/skills](https://github.com/ai-ecoverse/skills) (`skills/gmaps`). See NOTICE and
THIRD_PARTY_NOTICES. Licensed under the MIT licence (LICENSE).
