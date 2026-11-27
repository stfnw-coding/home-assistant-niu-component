"""WebSocket commands the ride-log card uses to read the ride history."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .rides import summarize

# Fields the card gets for every ride.
RIDE_FIELDS = (
    "id",
    "start",
    "end",
    "distance_m",
    "duration_s",
    "avg_speed",
    "consumption",
    "start_point",
    "end_point",
    "from",
    "to",
    "map",
    "map_source",
)


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_rides)


def _coordinator(hass: HomeAssistant, entry_id: str | None):
    entries = {
        key: value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if not key.startswith("_")
    }
    if entry_id:
        return entries.get(entry_id)
    return next(iter(entries.values()), None)


def public_ride(ride: dict) -> dict[str, Any]:
    public = {field: ride.get(field) for field in RIDE_FIELDS}
    if public["map"] and ride.get("map_drawn"):
        # Maps are served with a long cache time and keep their file name when
        # redrawn: a new URL per drawing makes browsers load the new map.
        stamp = "".join(ch for ch in ride["map_drawn"] if ch.isdigit())[:14]
        public["map"] = f"{public['map']}?v={stamp}"
    return public


@websocket_api.websocket_command(
    {
        vol.Required("type"): "niu/rides",
        vol.Optional("entry_id"): str,
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional("limit", default=20): vol.All(int, vol.Range(min=1, max=200)),
    }
)
@callback
def websocket_rides(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return a page of rides, newest first, with totals."""
    coordinator = _coordinator(hass, msg.get("entry_id"))
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "No NIU scooter is set up")
        return
    rides = coordinator.rides.ordered()
    offset, limit = msg["offset"], msg["limit"]
    connection.send_result(
        msg["id"],
        {
            "total": len(rides),
            "backfilling": coordinator.rides.backfilling,
            "summary": summarize(rides, dt_util.now()),
            "rides": [public_ride(ride) for ride in rides[offset : offset + limit]],
        },
    )
