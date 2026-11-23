"""Draw a map for every ride and name the places where rides start and end."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from pathlib import Path
from time import monotonic

import httpx

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

from .const import DOMAIN
from .map_render import TILE_STYLES, fit_viewport, render_route, tile_url
from .rides import RideLog, address_label, place_key, zone_name
from .thumb_route import route_from_thumbnail

_LOGGER = logging.getLogger(__name__)

MAP_DIR = "niu_ritten"
MAP_URL_PATH = "/niu_ritten"
MAP_WIDTH = 720
MAP_HEIGHT = 450
MAP_PADDING = 48
# Map renders per polling cycle, to stay friendly to the OpenStreetMap tile servers.
MAPS_PER_CYCLE = 4
# Address lookups per polling cycle; Nominatim allows one request per second.
PLACES_PER_CYCLE = 6
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = (
    "HomeAssistant-NIU-integration "
    "(+https://github.com/stfnw-coding/home-assistant-niu-component)"
)
TILE_TIMEOUT = 10
TILE_TOTAL_TIMEOUT = 25
TILE_CACHE_SIZE = 256
RETRY_FAILED_AFTER = 6 * 60 * 60


def _is_image(content: bytes) -> bool:
    return content.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")) or (
        content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    )


class RideMapper:
    """Works through the ride log, a few rides per polling cycle."""

    def __init__(self, hass: HomeAssistant, map_style: str) -> None:
        self.hass = hass
        self.style_name = map_style if map_style in TILE_STYLES else "dark"
        self.directory = Path(hass.config.path(MAP_DIR))
        self._failed: dict[str, float] = {}
        self._place_failed_until = 0.0

    async def async_process(self, log: RideLog, api) -> bool:
        """Draw missing maps and name missing places. Returns whether the log changed."""
        changed = await self._async_places(log)
        changed |= await self._async_maps(log, api)
        return changed

    # ---------------------------------------------------------------- places

    def _zones(self) -> list[dict]:
        zones = []
        for state in self.hass.states.async_all("zone"):
            attributes = state.attributes
            if attributes.get("passive"):
                continue
            zones.append(
                {
                    "name": state.name,
                    "latitude": attributes.get("latitude"),
                    "longitude": attributes.get("longitude"),
                    "radius": attributes.get("radius"),
                }
            )
        return zones

    async def _async_places(self, log: RideLog) -> bool:
        zones = self._zones()
        changed = False
        lookups = 0
        for ride in log.needing_places():
            for field, point_field in (("from", "start_point"), ("to", "end_point")):
                point = ride.get(point_field)
                if field in ride or not point:
                    continue
                name = zone_name(point, zones)
                if name is None:
                    key = place_key(point)
                    name = log.places.get(key)
                    if name is None:
                        if lookups >= PLACES_PER_CYCLE or monotonic() < self._place_failed_until:
                            continue
                        lookups += 1
                        name = await self._async_reverse(point)
                        if name is None:
                            continue
                        log.places[key] = name
                ride[field] = name
                changed = True
        return changed

    async def _async_reverse(self, point: list[float]) -> str | None:
        client = get_async_client(self.hass, verify_ssl=True)
        language = self.hass.config.language or "nl"
        try:
            await asyncio.sleep(1.1)
            response = await client.get(
                NOMINATIM_URL,
                params={
                    "lat": point[0],
                    "lon": point[1],
                    "format": "jsonv2",
                    "zoom": 18,
                    "addressdetails": 1,
                },
                headers={"User-Agent": USER_AGENT, "Accept-Language": language},
                timeout=10,
            )
            response.raise_for_status()
            label = address_label(response.json())
        except (httpx.HTTPError, ValueError) as err:
            _LOGGER.debug("Could not look up the address of a ride: %s", err)
            self._place_failed_until = monotonic() + 15 * 60
            return None
        return label

    # ------------------------------------------------------------------ maps

    async def _async_maps(self, log: RideLog, api) -> bool:
        changed = False
        drawn = 0
        for ride in log.needing_map():
            if drawn >= MAPS_PER_CYCLE:
                break
            failed_at = self._failed.get(ride["id"])
            if failed_at is not None and monotonic() - failed_at < RETRY_FAILED_AFTER:
                continue
            drawn += 1
            try:
                result = await self._async_draw(log, ride, api)
            except Exception:  # noqa: BLE001 - one ride must not stop the others
                _LOGGER.exception("Could not draw the map of NIU ride %s", ride["id"])
                result = None
            if result is None:
                self._failed[ride["id"]] = monotonic()
                continue
            ride["map"], ride["map_source"] = result
            changed = True
        return changed

    async def _async_draw(self, log: RideLog, ride: dict, api) -> tuple[str, str] | None:
        start, end = ride.get("start_point"), ride.get("end_point")
        ends = [tuple(start), tuple(end)] if start and end else None

        points = None
        if getattr(api, "track_points_id", None) == ride["id"]:
            points = list(getattr(api, "track_points", None) or []) or None

        thumbnail = None
        segments = None
        if points is None and ride.get("thumb"):
            thumbnail = await self._async_thumbnail(ride["thumb"])
            if thumbnail is not None and ends is not None:
                try:
                    segments = await self.hass.async_add_executor_job(
                        route_from_thumbnail, thumbnail, ends[0], ends[1]
                    )
                except Exception:  # noqa: BLE001
                    segments = None

        filename = log.map_filename(ride["id"])
        if points and len(points) >= 2:
            image, source = await self._async_render(points, None, True), "gps"
        elif segments and ends:
            image, source = await self._async_render(ends, segments, True), "thumbnail"
        elif thumbnail is not None:
            # NIU's own picture still shows the real route.
            image, source = thumbnail, "niu"
        elif ends:
            image, source = await self._async_render(ends, None, False), "markers"
        else:
            return None
        if image is None:
            return None
        if source == "niu" and not thumbnail.startswith(b"\xff\xd8\xff"):
            filename = filename.replace(".jpg", ".png")
        await self.hass.async_add_executor_job(self._write, filename, image)
        return f"{MAP_URL_PATH}/{filename}", source

    def _write(self, filename: str, image: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(image)
        tmp.replace(path)

    async def _async_render(self, points, segments, draw_line: bool) -> bytes | None:
        style = TILE_STYLES[self.style_name]
        extent = list(points)
        if segments:
            extent.extend(point for segment in segments for point in segment)
        viewport = fit_viewport(
            extent, style.tile_size, MAP_WIDTH, MAP_HEIGHT, MAP_PADDING
        )
        if len(extent) >= 2 and extent[0] == extent[-1] and not segments:
            # Start and end at one spot: do not zoom in all the way.
            viewport = fit_viewport(
                [(extent[0][0] - 0.003, extent[0][1] - 0.005),
                 (extent[0][0] + 0.003, extent[0][1] + 0.005)],
                style.tile_size, MAP_WIDTH, MAP_HEIGHT, MAP_PADDING,
            )
        tiles, _complete = await self._async_tiles(style, viewport.tiles())
        if not tiles:
            return None
        return await self.hass.async_add_executor_job(
            lambda: render_route(
                list(points),
                viewport,
                style,
                tiles,
                segments,
                draw_line=draw_line,
                image_format="JPEG",
            )
        )

    async def _async_tiles(self, style, wanted):
        cache: OrderedDict = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            "_tile_cache", OrderedDict()
        )
        client = get_async_client(self.hass, verify_ssl=True)
        result: dict = {}

        async def fetch(tile) -> None:
            cache_key = (style.url, *tile)
            if cache_key in cache:
                cache.move_to_end(cache_key)
                result[tile] = cache[cache_key]
                return
            try:
                response = await client.get(
                    tile_url(style, *tile),
                    timeout=TILE_TIMEOUT,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                )
                response.raise_for_status()
            except httpx.HTTPError:
                return
            if not _is_image(response.content):
                return
            result[tile] = response.content
            cache[cache_key] = response.content
            while len(cache) > TILE_CACHE_SIZE:
                cache.popitem(last=False)

        try:
            async with asyncio.timeout(TILE_TOTAL_TIMEOUT):
                await asyncio.gather(*(fetch(tile) for tile in wanted))
        except TimeoutError:
            _LOGGER.debug("Timed out loading map tiles for a NIU ride")
        return result, len(result) == len(wanted)

    async def _async_thumbnail(self, url: str) -> bytes | None:
        # Imported here: camera.py pulls in the camera platform.
        from .camera import _thumbnail_url_candidates

        client = get_async_client(self.hass, verify_ssl=True)
        for candidate in _thumbnail_url_candidates(_overseas(url)):
            try:
                response = await client.get(candidate, timeout=8, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            if response.content and _is_image(response.content):
                return response.content
        return None


def _overseas(url: str) -> str:
    """The same URL rewriting the last-track sensor applies."""
    if "app-api.niucache.com" in url:
        url = url.replace("app-api.niucache.com", "app-api-fk.niu.com")
    if "/track/thumb/" in url and "/track/overseas/thumb/" not in url:
        url = url.replace("/track/thumb/", "/track/overseas/thumb/")
    return url
