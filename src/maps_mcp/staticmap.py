"""Static map images from Google's map tiles, like the Static Maps API: an area around a centre,
a set of lettered pins, a route line. Pure rendering; tiles come from an injected async fetcher
(``GoogleMaps.tile``), so tests run offline.

Web Mercator, 256 px tiles: at zoom z the world is 256 * 2**z pixels wide.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

TILE = 256
MAX_LAT = 85.05112878
MAP_TYPES = {"roadmap": "m", "satellite": "s", "hybrid": "y", "terrain": "p"}
MIN_ZOOM, MAX_ZOOM = 1, 20
FIT_MAX_ZOOM = 18
PADDING = 48
ROUTE_BLUE = (26, 115, 232)
PIN_RED = (234, 67, 53)
WHITE = (255, 255, 255)
LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

TileFetcher = Callable[[str, int, int, int], Awaitable[bytes | None]]


@dataclass
class Pin:
    latitude: float
    longitude: float
    label: str = ""


@dataclass
class MapSpec:
    width: int = 640
    height: int = 480
    maptype: str = "roadmap"
    center: tuple[float, float] | None = None
    zoom: int | None = None
    pins: list[Pin] = field(default_factory=list)
    path: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class RenderedMap:
    image: bytes
    center: tuple[float, float]
    zoom: int
    width: int
    height: int
    tiles: int
    missing_tiles: int


def world_px(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    size = TILE * 2**zoom
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = (lng + 180.0) / 360.0 * size
    sin = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * size
    return x, y


def latlng(x: float, y: float, zoom: int) -> tuple[float, float]:
    size = TILE * 2**zoom
    lng = x / size * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y / size
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lng


def fit(points: list[tuple[float, float]], width: int, height: int, max_zoom: int = FIT_MAX_ZOOM) -> tuple:
    """The highest zoom at which every point fits inside the image minus padding, and the centre."""
    if len(points) == 1:
        return points[0], min(16, max_zoom)
    for zoom in range(max_zoom, MIN_ZOOM - 1, -1):
        xs, ys = zip(*(world_px(lat, lng, zoom) for lat, lng in points), strict=True)
        if max(xs) - min(xs) <= width - 2 * PADDING and max(ys) - min(ys) <= height - 2 * PADDING:
            return latlng((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, zoom), zoom
    xs, ys = zip(*(world_px(lat, lng, MIN_ZOOM) for lat, lng in points), strict=True)
    return latlng((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, MIN_ZOOM), MIN_ZOOM


def tiles_needed(center: tuple[float, float], zoom: int, width: int, height: int) -> list[tuple[int, int]]:
    cx, cy = world_px(*center, zoom)
    left, top = cx - width / 2, cy - height / 2
    count = 2**zoom
    out = []
    for ty in range(math.floor(top / TILE), math.floor((top + height - 1) / TILE) + 1):
        if 0 <= ty < count:
            for tx in range(math.floor(left / TILE), math.floor((left + width - 1) / TILE) + 1):
                out.append((tx, ty))
    return out


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow without FreeType
        return ImageFont.load_default()


async def render(spec: MapSpec, fetch: TileFetcher, *, concurrency: int = 4) -> RenderedMap:
    layer = MAP_TYPES[spec.maptype]
    points = [(p.latitude, p.longitude) for p in spec.pins] + list(spec.path)
    if spec.center is not None:
        center, zoom = spec.center, spec.zoom if spec.zoom is not None else 15
    elif points:
        center, fitted = fit(points, spec.width, spec.height)
        zoom = spec.zoom if spec.zoom is not None else fitted
    else:
        raise ValueError("a map needs a center, pins or a route")
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
    cx, cy = world_px(*center, zoom)
    left, top = cx - spec.width / 2, cy - spec.height / 2
    count = 2**zoom

    needed = tiles_needed(center, zoom, spec.width, spec.height)
    gate = asyncio.Semaphore(concurrency)

    async def one(tx: int, ty: int) -> tuple[int, int, bytes | None]:
        async with gate:
            return tx, ty, await fetch(layer, zoom, tx % count, ty)

    results = await asyncio.gather(*(one(tx, ty) for tx, ty in needed))
    canvas = Image.new("RGB", (spec.width, spec.height), (229, 227, 223))
    missing = 0
    for tx, ty, data in results:
        if not data:
            missing += 1
            continue
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB")
        except (OSError, ValueError):
            missing += 1
            continue
        if tile.size != (TILE, TILE):
            tile = tile.resize((TILE, TILE))
        canvas.paste(tile, (round(tx * TILE - left), round(ty * TILE - top)))

    draw = ImageDraw.Draw(canvas)

    def px(lat: float, lng: float) -> tuple[float, float]:
        x, y = world_px(lat, lng, zoom)
        return x - left, y - top

    if len(spec.path) >= 2:
        line = [px(lat, lng) for lat, lng in spec.path]
        draw.line(line, fill=WHITE, width=9, joint="curve")
        draw.line(line, fill=ROUTE_BLUE, width=5, joint="curve")
    pin_font = _font(13)
    for pin in spec.pins:
        x, y = px(pin.latitude, pin.longitude)
        r = 11
        draw.ellipse((x - r, y - r, x + r, y + r), fill=PIN_RED, outline=WHITE, width=2)
        if pin.label:
            draw.text((x, y), pin.label[:2], fill=WHITE, font=pin_font, anchor="mm")
    note = f"Map data ©{dt.date.today().year} Google"
    note_font = _font(11)
    box = draw.textbbox((0, 0), note, font=note_font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.rectangle((spec.width - w - 10, spec.height - h - 8, spec.width, spec.height), fill=WHITE)
    draw.text((spec.width - w - 5, spec.height - h - 5), note, fill=(68, 68, 68), font=note_font)

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88, optimize=True)
    return RenderedMap(
        image=out.getvalue(),
        center=center,
        zoom=zoom,
        width=spec.width,
        height=spec.height,
        tiles=len(needed),
        missing_tiles=missing,
    )
