"""Render a ride route on OpenStreetMap-based tiles."""

from __future__ import annotations

from dataclasses import dataclass
import io
import math

from .const import MAP_STYLE_DARK, MAP_STYLE_LIGHT, MAP_STYLE_OSM

IMAGE_WIDTH = 1024
IMAGE_HEIGHT = 640
PADDING = 64
MAX_ZOOM = 18
MIN_ZOOM = 2
SUPERSAMPLE = 3


@dataclass(frozen=True)
class TileStyle:
    """A raster tile source."""

    url: str
    tile_size: int
    attribution: str
    route_color: tuple[int, int, int]
    casing_color: tuple[int, int, int]
    background: tuple[int, int, int]
    label_background: tuple[int, int, int, int] = (255, 255, 255, 170)
    label_text: tuple[int, int, int] = (40, 40, 40)


TILE_STYLES: dict[str, TileStyle] = {
    MAP_STYLE_DARK: TileStyle(
        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        tile_size=512,
        attribution="© OpenStreetMap-bijdragers © CARTO",
        route_color=(77, 163, 255),
        casing_color=(10, 25, 50),
        background=(38, 38, 38),
        label_background=(0, 0, 0, 150),
        label_text=(200, 200, 200),
    ),
    MAP_STYLE_LIGHT: TileStyle(
        url="https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png",
        tile_size=512,
        attribution="© OpenStreetMap-bijdragers © CARTO",
        route_color=(37, 99, 235),
        casing_color=(255, 255, 255),
        background=(242, 239, 233),
    ),
    MAP_STYLE_OSM: TileStyle(
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        tile_size=256,
        attribution="© OpenStreetMap-bijdragers",
        route_color=(37, 99, 235),
        casing_color=(255, 255, 255),
        background=(242, 239, 233),
    ),
}

START_COLOR = (34, 197, 94)
END_COLOR = (239, 68, 68)


def project(lat: float, lng: float, zoom: int, tile_size: int) -> tuple[float, float]:
    """Project WGS84 coordinates to global Web Mercator pixels."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    scale = tile_size * (2**zoom)
    x = (lng + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


@dataclass(frozen=True)
class Viewport:
    """The map window that will be rendered."""

    zoom: int
    left: float
    top: float
    width: int
    height: int
    tile_size: int

    def tiles(self) -> list[tuple[int, int, int]]:
        """Return the (zoom, x, y) tiles covering the viewport."""
        count = 2**self.zoom
        first_x = math.floor(self.left / self.tile_size)
        last_x = math.floor((self.left + self.width - 1) / self.tile_size)
        first_y = max(0, math.floor(self.top / self.tile_size))
        last_y = min(count - 1, math.floor((self.top + self.height - 1) / self.tile_size))
        return [
            (self.zoom, x % count, y)
            for x in range(first_x, last_x + 1)
            for y in range(first_y, last_y + 1)
        ]

    def tile_offset(self, tile_x: int, tile_y: int) -> tuple[int, int]:
        """Return where a tile's top-left corner lands in the image.

        Tile x is unwrapped relative to the viewport so maps near the
        antimeridian still line up.
        """
        count = 2**self.zoom
        first_x = math.floor(self.left / self.tile_size)
        unwrapped_x = tile_x
        while unwrapped_x < first_x:
            unwrapped_x += count
        return (
            round(unwrapped_x * self.tile_size - self.left),
            round(tile_y * self.tile_size - self.top),
        )

    def to_image(self, lat: float, lng: float) -> tuple[float, float]:
        """Return image pixel coordinates for a point."""
        x, y = project(lat, lng, self.zoom, self.tile_size)
        return x - self.left, y - self.top


def fit_viewport(
    points: list[tuple[float, float]],
    tile_size: int,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    padding: int = PADDING,
) -> Viewport:
    """Pick the deepest zoom at which every point fits inside the image."""
    zoom = MIN_ZOOM
    for candidate in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        projected = [project(lat, lng, candidate, tile_size) for lat, lng in points]
        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        if (
            max(xs) - min(xs) <= width - 2 * padding
            and max(ys) - min(ys) <= height - 2 * padding
        ):
            zoom = candidate
            break

    projected = [project(lat, lng, zoom, tile_size) for lat, lng in points]
    xs = [p[0] for p in projected]
    ys = [p[1] for p in projected]
    centre_x = (min(xs) + max(xs)) / 2
    centre_y = (min(ys) + max(ys)) / 2
    return Viewport(
        zoom=zoom,
        left=centre_x - width / 2,
        top=centre_y - height / 2,
        width=width,
        height=height,
        tile_size=tile_size,
    )


def tile_url(style: TileStyle, zoom: int, x: int, y: int) -> str:
    """Return the URL of one tile."""
    subdomain = "abcd"[(x + y) % 4]
    return style.url.format(s=subdomain, z=zoom, x=x, y=y)


def render_route(
    points: list[tuple[float, float]],
    viewport: Viewport,
    style: TileStyle,
    tiles: dict[tuple[int, int, int], bytes],
    segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> bytes:
    """Compose tiles, route and markers into a PNG image.

    The route is either the polyline through ``points`` or, when given,
    ``segments`` (pairs of coordinates); the start and end markers go on the
    first and last of ``points``.
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = viewport.width, viewport.height
    image = Image.new("RGB", (width, height), style.background)

    for (zoom, tile_x, tile_y), content in tiles.items():
        if zoom != viewport.zoom or not content:
            continue
        try:
            tile = Image.open(io.BytesIO(content)).convert("RGB")
        except Exception:  # noqa: BLE001 - one broken tile must not break the map
            continue
        if tile.size != (viewport.tile_size, viewport.tile_size):
            tile = tile.resize((viewport.tile_size, viewport.tile_size))
        image.paste(tile, viewport.tile_offset(tile_x, tile_y))

    # Draw the route supersampled so the line comes out smooth.
    factor = SUPERSAMPLE
    overlay = Image.new("RGBA", (width * factor, height * factor), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    coords = [
        (x * factor, y * factor)
        for x, y in (viewport.to_image(lat, lng) for lat, lng in points)
    ]

    def to_overlay(lat: float, lng: float) -> tuple[float, float]:
        x, y = viewport.to_image(lat, lng)
        return x * factor, y * factor

    def stroke(color: tuple[int, ...], line_width: int) -> None:
        radius = line_width / 2
        if segments:
            for first, second in segments:
                a, b = to_overlay(*first), to_overlay(*second)
                draw.line((a, b), fill=color, width=line_width)
                draw.ellipse(
                    (a[0] - radius, a[1] - radius, a[0] + radius, a[1] + radius),
                    fill=color,
                )
            return
        draw.line(coords, fill=color, width=line_width, joint="curve")
        for x, y in (coords[0], coords[-1]):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    stroke((*style.casing_color, 200), 11 * factor)
    stroke((*style.route_color, 255), 6 * factor)

    for (x, y), color in ((coords[0], START_COLOR), (coords[-1], END_COLOR)):
        outer = 11 * factor
        inner = 7 * factor
        draw.ellipse((x - outer, y - outer, x + outer, y + outer), fill=(255, 255, 255, 255))
        draw.ellipse((x - inner, y - inner, x + inner, y + inner), fill=(*color, 255))

    overlay = overlay.resize((width, height), Image.Resampling.LANCZOS)
    image = Image.alpha_composite(image.convert("RGBA"), overlay)

    # Attribution, required by the tile providers.
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    text = style.attribution
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_width, text_height = right - left, bottom - top
    box = (
        width - text_width - 14,
        height - text_height - 12,
        width,
        height,
    )
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(box, fill=style.label_background)
    image = Image.alpha_composite(image, overlay)
    ImageDraw.Draw(image).text(
        (box[0] + 7 - left, box[1] + 6 - top), text, fill=style.label_text, font=font
    )

    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
