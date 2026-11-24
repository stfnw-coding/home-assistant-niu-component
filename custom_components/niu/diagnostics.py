"""Diagnostics for the NIU integration: the raw NIU responses, redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH, DOMAIN

TO_REDACT = {
    "password",
    "username",
    CONF_AUTH,
    "token",
    "access_token",
    "refresh_token",
    "sn",
    "sn_id",
    "carframe_id",
    "lat",
    "lng",
    "latitude",
    "longitude",
    "postion",
    "startPoint",
    "lastPoint",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    api = coordinator.api
    return async_redact_data(
        {
            "motor_index_info": api.dataMoto,
            "battery_info": api.dataBat,
            "overall_tally": api.dataMotoInfo,
            "newest_tracks": (api.dataTrackInfo or {}).get("data")
            if isinstance(api.dataTrackInfo, dict)
            else None,
            "messages": coordinator.messages,
            "messages_raw": coordinator.messages_raw,
            "firmware": coordinator.firmware,
            "battery_chart_points": len(coordinator.battery_chart or []),
            "ride_log": {
                "rides": len(coordinator.rides.rides),
                "backfilling": coordinator.rides.backfilling,
                "trace": coordinator.rides.backfill_trace,
            },
        },
        TO_REDACT,
    )
