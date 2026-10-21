#!/usr/bin/env bash
# Refresh src/maps_mcp/vendor/gmaps from benjamin-di/gmaps-scraper at a commit (default: the
# maps-mcp branch head). The fork = upstream nick-choudhary/gmaps-scraper + this project's PRs.
# Only the modules maps-mcp imports are kept (see below).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
ref="${1:-maps-mcp}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone -q https://github.com/benjamin-di/gmaps-scraper.git "$work/src"
git -C "$work/src" checkout -q "$ref"
commit="$(git -C "$work/src" rev-parse HEAD)"
dest="$root/src/maps_mcp/vendor/gmaps"
rm -rf "$dest"
mkdir -p "$dest"
cp -R "$work/src/src/gmaps/." "$dest/"
# Only what maps-mcp runs: the client, search, places, directions and their RPC layer. The
# lead-generation parts (contact and website extraction, third-party fetchers, geocoding,
# grid coverage, browser identity capture), the CLI and the stdio MCP server stay out.
keep="__init__.py client.py _search.py _places.py _directions.py _reviews.py control.py exceptions.py
grid.py transport.py validation.py _hosts.py _auth rpc"
for entry in "$dest"/*; do
    name="$(basename "$entry")"
    case " $(echo $keep) " in *" $name "*) ;; *) rm -rf "$entry" ;; esac
done
find "$dest" -name __pycache__ -prune -exec rm -rf {} +
cp "$work/src/LICENSE" "$dest/LICENSE"
printf '%s\n' "$commit" > "$dest/VENDORED_COMMIT"
echo "vendored gmaps-scraper $commit into src/maps_mcp/vendor/gmaps"
