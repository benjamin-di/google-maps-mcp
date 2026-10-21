"""Stop ordering for multi-stop routes.

Google's web directions have no "optimize waypoints" switch, and a full travel-time matrix costs
one request per pair. So: rank every order of the stops by straight-line (haversine) length,
then let real Google routes decide between the few best of those plus the caller's own order
(``candidate_orders``). With up to 8 stops that is at most 40,320 orders to rank, which takes
milliseconds; the real routes cost one request each.
"""

from __future__ import annotations

import itertools
import math

Point = tuple[float, float]
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(a: Point, b: Point) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def path_length(points: list[Point]) -> float:
    return sum(haversine_m(a, b) for a, b in itertools.pairwise(points))


def candidate_orders(
    origin: Point,
    stops: list[Point],
    end: Point | None,
    *,
    keep: int = 4,
) -> list[tuple[int, ...]]:
    """The ``keep`` shortest stop orders by straight-line length, plus the given order.

    ``end`` is a fixed destination (or the origin again for a round trip); None means the
    route ends at whichever stop comes last. Returns tuples of stop indices, the given order
    (0, 1, …) always included, the shortest first.
    """
    n = len(stops)
    given = tuple(range(n))
    if n <= 1:
        return [given]

    def length(order: tuple[int, ...]) -> float:
        points = [origin, *(stops[i] for i in order)]
        if end is not None:
            points.append(end)
        return path_length(points)

    ranked = sorted(itertools.permutations(range(n)), key=length)
    chosen = ranked[:keep]
    if given not in chosen:
        chosen.append(given)
    return chosen
