"""NIU scooter integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store

from .api import NiuApi
from .battery import STORAGE_VERSION, BatteryRegistry
from .const import (
    CONF_AUTH,
    CONF_MAP_STYLE,
    CONF_PASSWORD,
    CONF_SCOOTER_ID,
    CONF_SENSORS,
    CONF_USERNAME,
    DEFAULT_MAP_STYLE,
    DOMAIN,
    SENSOR_TYPE_BAT,
    SENSOR_TYPE_MOTO,
    SENSOR_TYPE_POS,
    SENSOR_TYPE_TRACK,
)
from .coordinator import NiuDataUpdateCoordinator, required_sensor_groups
from .ride_maps import MAP_DIR, MAP_URL_PATH, RideMapper
from .rides import STORAGE_VERSION as RIDES_STORAGE_VERSION, RideLog
from .websocket import async_register_websocket_commands

FRONTEND_URL_PATH = "/niu_frontend"
CARD_FILE = "niu-rides-card.js"

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Niu component."""
    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)
    async_register_websocket_commands(hass)
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    async def reread_ride_history(call: ServiceCall) -> None:
        for key, coordinator in hass.data.get(DOMAIN, {}).items():
            if key.startswith("_"):
                continue
            coordinator.rides.restart_backfill()
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "reread_ride_history", reread_ride_history)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the ride maps and the ride-log dashboard card."""
    map_dir = Path(hass.config.path(MAP_DIR))
    await hass.async_add_executor_job(lambda: map_dir.mkdir(exist_ok=True))
    frontend_dir = Path(__file__).parent / "frontend"
    version = await hass.async_add_executor_job(
        lambda: int((frontend_dir / CARD_FILE).stat().st_mtime)
    )
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(MAP_URL_PATH, str(map_dir), True),
            StaticPathConfig(FRONTEND_URL_PATH, str(frontend_dir), False),
        ]
    )
    if "frontend" in hass.config.components:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{FRONTEND_URL_PATH}/{CARD_FILE}?v={version}")


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
    rides_store = Store(
        hass, RIDES_STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.rides"
    )
    rides = RideLog(await rides_store.async_load())
    mapper = RideMapper(hass, entry.options.get(CONF_MAP_STYLE, DEFAULT_MAP_STYLE))
    coordinator = NiuDataUpdateCoordinator(
        hass,
        entry,
        api,
        # Location, status, rides and per-battery entities are always available.
        required_sensor_groups(sensors_selected)
        | {SENSOR_TYPE_BAT, SENSOR_TYPE_POS, SENSOR_TYPE_MOTO, SENSOR_TYPE_TRACK},
        batteries,
        battery_store,
        rides,
        rides_store,
        mapper,
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
        return ("sensor", "binary_sensor", "device_tracker", "camera")
    return ("sensor", "binary_sensor", "device_tracker")
