"""A persistent log of the scooter's rides."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import math
import secrets
from typing import Any

STORAGE_VERSION = 1
# Rides kept in the log; about a year of daily commuting.
MAX_RIDES = 1000
# Rides this recent are checked against Home Assistant's position history
# (about the recorder's default keep period).
TRAIL_DAYS = 9
# NIU's route counts as complete when it covers this share of the ride.
ROUTE_COMPLETE_SHARE = 0.6
# Bumped when the route check improves, so recent rides are checked again.
ROUTE_CHECK_VERSION = 3
# How far back the first backfill reads the ride history.
BACKFILL_DAYS = 120
# NIU's ride list is paged: "index" is the page number, not an offset.
BACKFILL_MODE = "page"
# Ride-list pages read per backfill step (one step per polling cycle).
BACKFILL_PAGES_PER_STEP = 4
BACKFILL_MAX_PAGES = 120


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _point(value) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    lat, lng = _number(value.get("lat")), _number(value.get("lng"))
    if lat is None or lng is None or (lat == 0 and lng == 0):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return [round(lat, 6), round(lng, 6)]


def _iso(ms: float | None) -> str | None:
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def ride_from_track(track: dict) -> dict[str, Any] | None:
    """Turn one /v5/track/list/v2 item into a ride record."""
    track_id = track.get("trackId")
    start_ms = _number(track.get("startTime"))
    if not track_id or start_ms is None:
        return None
    end_ms = _number(track.get("endTime"))
    duration = _number(track.get("ridingtime"))
    if duration is None and end_ms is not None:
        duration = max(0.0, (end_ms - start_ms) / 1000)
    distance = _number(track.get("distance"))
    thumb = track.get("track_thumb")
    return {
        "id": str(track_id),
        "start": _iso(start_ms),
        "end": _iso(end_ms),
        "date": str(track.get("date") or ""),
        "distance_m": round(distance) if distance is not None else None,
        "duration_s": round(duration) if duration is not None else None,
        "avg_speed": _number(track.get("avespeed")),
        "consumption": _number(track.get("power_consumption")),
        "start_point": _point(track.get("startPoint")),
        "end_point": _point(track.get("lastPoint")),
        "thumb": thumb if isinstance(thumb, str) and thumb else None,
    }


# Fields that come from NIU and are refreshed when a ride is seen again.
_NIU_FIELDS = (
    "start",
    "end",
    "date",
    "distance_m",
    "duration_s",
    "avg_speed",
    "consumption",
    "start_point",
    "end_point",
    "thumb",
)


# A map drawn this soon after the ride ended may miss the last positions.
MAP_SETTLE = timedelta(minutes=5)


class RideLog:
    """All rides seen so far, with the map and place names worked out for them."""

    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        rides = data.get("rides")
        self.rides: dict[str, dict] = (
            {ride["id"]: ride for ride in rides if isinstance(ride, dict) and "id" in ride}
            if isinstance(rides, list)
            else {}
        )
        self.salt: str = data.get("salt") or secrets.token_hex(8)
        self.backfill_index: int | None = data.get("backfill_index", 0)
        self.backfill_empty: int = data.get("backfill_empty", 0)
        self.backfill_trace: list[dict] = list(data.get("backfill_trace") or [])
        if data and data.get("backfill_mode") != BACKFILL_MODE:
            # Earlier logs paged through the history wrongly and missed rides.
            self.restart_backfill()
        self.places: dict[str, str] = dict(data.get("places") or {})

    def as_dict(self) -> dict:
        return {
            "rides": self.ordered(),
            "salt": self.salt,
            "backfill_index": self.backfill_index,
            "backfill_empty": self.backfill_empty,
            "backfill_trace": self.backfill_trace,
            "backfill_mode": BACKFILL_MODE,
            "places": self.places,
        }

    def ordered(self) -> list[dict]:
        """Rides newest first."""
        return sorted(
            self.rides.values(), key=lambda ride: ride.get("start") or "", reverse=True
        )

    def merge(self, tracks: list[dict]) -> tuple[list[str], bool]:
        """Add or refresh rides; return the ids of new rides and whether anything changed."""
        new_ids: list[str] = []
        changed = False
        for track in tracks:
            ride = ride_from_track(track)
            if ride is None:
                continue
            existing = self.rides.get(ride["id"])
            if existing is None:
                self.rides[ride["id"]] = ride
                new_ids.append(ride["id"])
                changed = True
                continue
            for field in _NIU_FIELDS:
                if field == "end_point" and existing.get("end_corrected"):
                    # NIU's end point was wrong; keep the corrected one, but
                    # remember NIU's latest for when the route is checked again.
                    if ride[field] is not None and existing.get("niu_end_point") != ride[field]:
                        existing["niu_end_point"] = ride[field]
                        changed = True
                    continue
                if ride[field] is not None and existing.get(field) != ride[field]:
                    existing[field] = ride[field]
                    changed = True
                    if field in ("thumb", "start_point", "end_point"):
                        # The route changed: draw the map again.
                        existing.pop("map", None)
                        existing.pop("map_source", None)
                        existing.pop("route_checked", None)
                    elif field in ("end", "distance_m"):
                        # The ride was still going on when it was first seen:
                        # check the route again, keeping the map until then.
                        existing.pop("route_checked", None)
                    # A moved start or end point needs its place name again.
                    if field == "start_point":
                        existing.pop("from", None)
                    if field == "end_point":
                        existing.pop("to", None)
        if len(self.rides) > MAX_RIDES:
            for ride in self.ordered()[MAX_RIDES:]:
                del self.rides[ride["id"]]
            changed = True
        return new_ids, changed

    def map_filename(self, ride_id: str) -> str:
        """A file name that cannot be guessed from the ride id."""
        digest = hashlib.sha256(f"{self.salt}:{ride_id}".encode()).hexdigest()[:24]
        return f"{digest}.jpg"

    def needing_map(self, now: datetime | None = None) -> list[dict]:
        """Rides without a map, then recent rides whose route was not checked
        against Home Assistant's own position history yet."""
        missing = [ride for ride in self.ordered() if not ride.get("map")]
        if now is None:
            return missing
        recent = (now - timedelta(days=TRAIL_DAYS)).isoformat()
        unchecked = [
            ride
            for ride in self.ordered()
            if ride.get("map")
            and (ride.get("start") or "") >= recent
            and (
                ride.get("route_checked") != ROUTE_CHECK_VERSION
                or drawn_too_early(ride, now)
            )
        ]
        return missing + unchecked

    def needing_places(self) -> list[dict]:
        return [
            ride
            for ride in self.ordered()
            if place_outdated(ride, "from", "start_point")
            or place_outdated(ride, "to", "end_point")
        ]

    @property
    def backfilling(self) -> bool:
        return self.backfill_index is not None

    def backfill_page(self, tracks: list[dict], page_size: int, now: datetime) -> None:
        """Account for one page of older rides read during the backfill.

        The backfill stops after two empty pages in a row (the end of the
        history), once a whole page is older than ``BACKFILL_DAYS``, or after
        ``BACKFILL_MAX_PAGES``. A short page alone does not stop it: NIU
        sometimes returns fewer rides than asked for in the middle of the
        history.
        """
        assert self.backfill_index is not None
        index = self.backfill_index
        self.backfill_index += 1
        starts = [
            _number(track.get("startTime")) for track in tracks if isinstance(track, dict)
        ]
        starts = [start for start in starts if start is not None]
        self.backfill_trace.append(
            {
                "page": index,
                "count": len(tracks),
                "newest": _iso(max(starts)) if starts else None,
                "oldest": _iso(min(starts)) if starts else None,
            }
        )
        del self.backfill_trace[:-60]
        self.backfill_empty = 0 if tracks else self.backfill_empty + 1
        oldest_wanted = now - timedelta(days=BACKFILL_DAYS)
        if (
            self.backfill_empty >= 2
            or (starts and max(starts) / 1000 < oldest_wanted.timestamp())
            or self.backfill_index >= BACKFILL_MAX_PAGES
        ):
            self.backfill_index = None

    def restart_backfill(self) -> None:
        """Read the ride history again from the newest ride."""
        self.backfill_index = 0
        self.backfill_empty = 0
        self.backfill_trace = []


def place_outdated(ride: dict, field: str, point_field: str) -> bool:
    """Whether a ride's place name is missing or belongs to an older point."""
    point = ride.get(point_field)
    if not point:
        return False
    return field not in ride or ride.get(f"{field}_point") != point


def place_key(point: list[float]) -> str:
    """Cache key for a place name: about 10 m resolution."""
    return f"{point[0]:.4f},{point[1]:.4f}"


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371000 * math.asin(math.sqrt(h))


def path_length(points) -> float:
    """Length in metres of a polyline of (lat, lng) points."""
    return sum(
        distance_m(tuple(a), tuple(b)) for a, b in zip(points, points[1:])
    )


def segments_length(segments) -> float:
    return sum(distance_m(tuple(a), tuple(b)) for a, b in segments or [])


def choose_route(
    ride: dict,
    niu_points: list | None,
    niu_segments: list | None,
    trail: list,
) -> tuple[str, list | None]:
    """Pick the best route source for a ride.

    Returns ``(source, points)``: ``"niu"`` keeps NIU's own route (GPS
    points or the traced thumbnail); ``"ha"`` uses the positions Home
    Assistant recorded during the ride, with the ride's start in front.
    NIU sometimes stores only the first part of a ride (and puts the end
    point there); Home Assistant's own trail then shows where it went.
    """
    expected = ride.get("distance_m") or 0
    if niu_points and len(niu_points) >= 2:
        niu_length = path_length(niu_points)
    elif niu_segments:
        niu_length = segments_length(niu_segments)
    elif ride.get("start_point") and ride.get("end_point"):
        niu_length = distance_m(tuple(ride["start_point"]), tuple(ride["end_point"]))
    else:
        niu_length = 0.0
    if not expected or niu_length >= ROUTE_COMPLETE_SHARE * expected:
        return "niu", None

    route = []
    for point in ([ride["start_point"]] if ride.get("start_point") else []) + list(trail):
        point = [round(point[0], 6), round(point[1], 6)]
        if not route or point != route[-1]:
            route.append(point)
    if len(route) >= 2 and path_length(route) > niu_length * 1.3:
        return "ha", route
    return "niu", None


def drawn_too_early(ride: dict, now: datetime) -> bool:
    """Whether the map was drawn before the ride's last positions were in."""
    drawn = _parse(ride.get("map_drawn"))
    end = _parse(ride.get("end"))
    if drawn is None or end is None:
        return False
    return drawn < end + MAP_SETTLE <= now


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def zone_name(point: list[float], zones: list[dict]) -> str | None:
    """Name of the smallest zone the point lies in."""
    best: tuple[float, str] | None = None
    for zone in zones:
        try:
            centre = (float(zone["latitude"]), float(zone["longitude"]))
            radius = float(zone.get("radius") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        # Allow some GPS slack around the zone edge.
        if distance_m((point[0], point[1]), centre) <= radius + 50:
            if best is None or radius < best[0]:
                best = (radius, zone["name"])
    return best[1] if best else None


def address_label(payload: dict) -> str | None:
    """A short "Street 12, Town" label from a Nominatim reverse response."""
    if not isinstance(payload, dict):
        return None
    address = payload.get("address")
    if not isinstance(address, dict):
        return payload.get("name") or None
    street = next(
        (
            address[key]
            for key in ("road", "pedestrian", "cycleway", "footway", "path", "square")
            if address.get(key)
        ),
        None,
    )
    number = address.get("house_number")
    town = next(
        (
            address[key]
            for key in ("city", "town", "village", "municipality")
            if address.get(key)
        ),
        None,
    )
    area = address.get("suburb") or address.get("neighbourhood")
    first = f"{street} {number}" if street and number else street or area or payload.get("name")
    if first and town:
        return f"{first}, {town}"
    return first or town


def summarize(rides: list[dict], now: datetime) -> dict[str, Any]:
    """Totals for today, this week and this month (local time ``now``)."""
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    tz = now.tzinfo
    totals = {
        key: {"rides": 0, "km": 0.0}
        for key in ("today", "week", "month")
    }
    for ride in rides:
        try:
            day = datetime.fromisoformat(ride["start"]).astimezone(tz).date()
        except (KeyError, TypeError, ValueError):
            continue
        km = (ride.get("distance_m") or 0) / 1000
        for key, first_day in (("today", today), ("week", week_start), ("month", month_start)):
            if first_day <= day <= today:
                totals[key]["rides"] += 1
                totals[key]["km"] += km
    for total in totals.values():
        total["km"] = round(total["km"], 1)
    return totals
