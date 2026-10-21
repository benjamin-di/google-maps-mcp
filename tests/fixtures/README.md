# Recorded fixtures

Responses from google.com/maps, recorded signed out by `scripts/capture-fixtures` through the
vendored library exactly as maps-mcp requests them, then scrubbed: photo URLs and media keys,
per-request tokens and every non-Google URL are replaced by fixed placeholders
(`token-0001`, `https://lh3.googleusercontent.com/p/example-photo…`, `https://www.example.com/`).
What remains is Google's public data about public places. Files are JSON with one value per
line.

| File | Request |
|---|---|
| `walk_text.json` | Wien Hauptbahnhof → Belvedere, walking, by text |
| `walk_coords.json` | two coordinates in the old town, walking |
| `drive_3_stop.json` | Schönbrunn → Naschmarkt → Riesenrad, driving |
| `drive_depart.json` / `drive_arrive.json` | Vienna International Airport → Stephansplatz, depart 08:30 / arrive by 09:00 on a Thursday, with traffic and alternatives |
| `unknown.json` | an unresolvable stop → Albertina |
| `noroute_drive.json` | Vienna → New York, driving |
| `korea_walk.json` | two beaches in Busan: Google has no walking routes in South Korea |
| `search_museums.json` | "museum" near the Albertina |
| `search_single.json` | a query naming one café exactly (single-result envelope) |
| `details_albertina.json`, `details_cafe.json` | place details; the café's details carry no hours |
| `hours_samples.json` | dated opening hours of six places (21-27 Oct 2026): a confirmed closure on Austrian National Day, a "might affect these hours" notice, 24 hours, closing at 04:00, lunch and dinner sittings |

Re-record with `.venv/bin/python scripts/capture-fixtures [--only NAME ...]`, review the diff,
then update the tests' expectations.
