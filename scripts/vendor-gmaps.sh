#!/usr/bin/env bash
set -euo pipefail
if (( $# != 1 )); then
    echo "Usage: scripts/vendor-gmaps.sh FULL_COMMIT_SHA (update fleet/gmaps-source.json first)" >&2
    exit 64
fi
root="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$root/scripts/verify-vendor.py" --refresh "$1"
