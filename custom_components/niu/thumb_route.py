"""Recover a ride's route from NIU's own track thumbnail.

NIU's track detail endpoint no longer returns GPS points for overseas
accounts, but its thumbnail (drawn on AMap, which has no streets outside
China) still shows the exact route. The thumbnail is a north-up Web
Mercator image, so the start and end coordinates from the track list and
the positions of the start and end pins in the picture fix a similarity
transform. With it, every pixel of the drawn route maps to a coordinate,
and the route can be redrawn on OpenStreetMap tiles.
"""

from __future__ import annotations

import cmath
import io
import math

from .map_render import project

LINE_COLOR = (66, 107, 242)
LINE_TOLERANCE = 30
END_PIN_COLOR = (223, 24, 43)
END_PIN_TOLERANCE = 40
MAX_ROTATION_DEGREES = 15
MIN_PIN_DISTANCE = 10

Segment = tuple[tuple[float, float], tuple[float, float]]


def _close(color, target, tolerance) -> bool:
    return sum((a - b) ** 2 for a, b in zip(color, target)) <= tolerance**2


def _start_pin(color) -> bool:
    red, green, blue = color
    return blue > 235 and 85 <= red <= 135 and 125 <= green <= 175


def _components(pixels: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    remaining = set(pixels)
    found = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        component = [seed]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (x + dx, y + dy)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        stack.append(neighbour)
                        component.append(neighbour)
        found.append(component)
    return sorted(found, key=len, reverse=True)


def _thin(pixels: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Zhang-Suen thinning: reduce the drawn line to a one-pixel skeleton."""
    skeleton = set(pixels)

    def neighbours(x, y):
        return [
            (x, y - 1) in skeleton,
            (x + 1, y - 1) in skeleton,
            (x + 1, y) in skeleton,
            (x + 1, y + 1) in skeleton,
            (x, y + 1) in skeleton,
            (x - 1, y + 1) in skeleton,
            (x - 1, y) in skeleton,
            (x - 1, y - 1) in skeleton,
        ]

    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            remove = []
            for x, y in skeleton:
                n = neighbours(x, y)
                count = sum(n)
                if not 2 <= count <= 6:
                    continue
                transitions = sum(
                    1 for i in range(8) if not n[i] and n[(i + 1) % 8]
                )
                if transitions != 1:
                    continue
                p2, p4, p6, p8 = n[0], n[2], n[4], n[6]
                if step == 0 and (p2 and p4 and p6 or p4 and p6 and p8):
                    continue
                if step == 1 and (p2 and p4 and p8 or p2 and p6 and p8):
                    continue
                remove.append((x, y))
            if remove:
                skeleton.difference_update(remove)
                changed = True
    return skeleton


def _unproject(x: float, y: float) -> tuple[float, float]:
    """Inverse of project() at zoom 0 with 256-pixel tiles."""
    lng = x / 256 * 360 - 180
    n = math.pi - 2 * math.pi * y / 256
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lng


def route_from_thumbnail(
    image: bytes,
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[Segment] | None:
    """Return the route drawn in a NIU thumbnail as coordinate segments."""
    from PIL import Image

    picture = Image.open(io.BytesIO(image)).convert("RGB")
    width, height = picture.size
    pixels = picture.load()

    def in_logo(x, y):
        return y > height * 0.88 and x < width * 0.6

    line: set[tuple[int, int]] = set()
    end_pin: set[tuple[int, int]] = set()
    start_pin: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            if in_logo(x, y):
                continue
            color = pixels[x, y]
            if _close(color, LINE_COLOR, LINE_TOLERANCE):
                line.add((x, y))
            elif _close(color, END_PIN_COLOR, END_PIN_TOLERANCE):
                end_pin.add((x, y))
            elif _start_pin(color):
                start_pin.add((x, y))

    if len(line) < 20 or not end_pin or not start_pin:
        return None

    end_blob = _components(end_pin)[0]
    bottom = max(y for _, y in end_blob)
    tip_row = [x for x, y in end_blob if y >= bottom - 1]
    end_px = complex(sum(tip_row) / len(tip_row), bottom)

    # The start pin's tip is hidden where it meets the line: use the line
    # pixel closest to the bottom of the pin's head.
    start_blob = _components(start_pin)[0]
    head_bottom = max(y for _, y in start_blob)
    head_x = sum(x for x, _ in start_blob) / len(start_blob)
    anchor = complex(head_x, head_bottom)
    start_px = min(
        (complex(x, y) for x, y in line), key=lambda p: abs(p - anchor)
    )
    if abs(end_px - start_px) < MIN_PIN_DISTANCE:
        return None

    start_m = complex(*project(*start, 0, 256))
    end_m = complex(*project(*end, 0, 256))
    if abs(end_m - start_m) == 0:
        return None
    scale = (end_px - start_px) / (end_m - start_m)
    if abs(math.degrees(cmath.phase(scale))) > MAX_ROTATION_DEGREES:
        return None
    # North-up map: keep the scale, drop the small rotation from pin noise.
    scale = complex(abs(scale), 0)
    offset = ((start_px - scale * start_m) + (end_px - scale * end_m)) / 2

    def to_coordinate(x: int, y: int) -> tuple[float, float]:
        mercator = (complex(x, y) - offset) / scale
        return _unproject(mercator.real, mercator.imag)

    # Keep the line pieces that belong to the route, not stray pixels.
    route = set()
    for component in _components(line):
        if len(component) >= 15:
            route.update(component)
    skeleton = _thin(route)

    segments: list[Segment] = []
    for x, y in skeleton:
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            if (x + dx, y + dy) in skeleton:
                segments.append((to_coordinate(x, y), to_coordinate(x + dx, y + dy)))
    return segments or None
