"""NIU scooter integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import NiuApi
from .battery import STORAGE_VERSION, BatteryRegistry
from .const import (
    CONF_AUTH,
    CONF_PASSWORD,
    CONF_SCOOTER_ID,
    CONF_SENSORS,
    CONF_USERNAME,
    DOMAIN,
    SENSOR_TYPE_BAT,
    SENSOR_TYPE_POS,
)
from .coordinator import NiuDataUpdateCoordinator, required_sensor_groups

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Niu component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NIU from a config entry."""
    niu_auth = entry.data.get(CONF_AUTH)
    if niu_auth is None:
        return False

    sensors_selected = niu_auth.get(CONF_SENSORS, [])
    if not sensors_selected:
        _LOGGER.error("No NIU sensors selected; integration cannot be set up")
        return False

    api = NiuApi.from_hass(
        hass,
        niu_auth[CONF_USERNAME],
        niu_auth[CONF_PASSWORD],
        niu_auth[CONF_SCOOTER_ID],
    )
    battery_store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.batteries")
    batteries = BatteryRegistry(await battery_store.async_load())
    coordinator = NiuDataUpdateCoordinator(
        hass,
        entry,
        api,
        # Location and per-battery entities are always available.
        required_sensor_groups(sensors_selected) | {SENSOR_TYPE_BAT, SENSOR_TYPE_POS},
        batteries,
        battery_store,
    )
    await coordinator.async_config_entry_first_refresh()

    entry_updates = {}
    if entry.unique_id is None:
        entry_updates["unique_id"] = str(api.sn)
    if entry.title == "Niu EScooter Integration":
        entry_updates["title"] = f"NIU – {api.sensor_prefix}"
    if entry_updates:
        hass.config_entries.async_update_entry(entry, **entry_updates)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(
        entry, _get_platforms(sensors_selected)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload so a new map style is used right away."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading NIU config entry")
    niu_auth = entry.data.get(CONF_AUTH, {})
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, _get_platforms(niu_auth.get(CONF_SENSORS, []))
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


def _get_platforms(sensors_selected: list[str]) -> tuple[str, ...]:
    """Return an immutable, per-entry platform list."""
    if "LastTrackThumb" in sensors_selected:
        return ("sensor", "device_tracker", "camera")
    return ("sensor", "device_tracker")
