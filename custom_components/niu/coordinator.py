"""Coordinate NIU API polling for all entities in a config entry."""

from __future__ import annotations

from datetime import timedelta
import logging
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from homeassistant.util import dt as dt_util

from .api import TRACK_PAGE_SIZE, NiuApi, NiuApiError, NiuRateLimitError, track_items
from .battery import BatteryRegistry
from .rides import BACKFILL_PAGES_PER_STEP, RideLog, distance_m
from .const import (
    DOMAIN,
    EVENT_NIU_MESSAGE,
    SIGNAL_NEW_BATTERY,
    SENSOR_TYPE_BAT,
    SENSOR_TYPE_BAT2,
    SENSOR_TYPE_DIST,
    SENSOR_TYPE_MOTO,
    SENSOR_TYPE_OVERALL,
    SENSOR_TYPE_POS,
    SENSOR_TYPE_TRACK,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=150)
# Poll faster while the scooter is switched on: more positions for the ride map.
RIDING_POLL_INTERVAL = timedelta(seconds=60)
# Keep polling fast this long after the scooter last moved.
RIDING_LINGER = 5 * 60
MOVED_METERS = 80
# While NIU's cloud has a hiccup, keep showing the last data this long.
KEEP_LAST_DATA = timedelta(hours=6)
BATTERY_SAVE_DELAY = 60
DEFAULT_RATE_LIMIT_BACKOFF = 300
MIN_RATE_LIMIT_BACKOFF = 30
MAX_RATE_LIMIT_BACKOFF = 3600
RIDES_SAVE_DELAY = 30
# How often the slower-changing extras are fetched.
MILEAGE_INTERVAL = 60 * 60
FIRMWARE_INTERVAL = 12 * 60 * 60
MESSAGES_INTERVAL = 5 * 60
BATTERY_HEALTH_INTERVAL = 6 * 60 * 60
BATTERY_CHART_INTERVAL = 60 * 60


def required_sensor_groups(sensors_selected: list[str]) -> set[str]:
    """Return the API data groups needed by the selected entities."""
    return {
        SENSOR_TYPES[sensor][3]
        for sensor in sensors_selected
        if sensor in SENSOR_TYPES
    }


class NiuDataUpdateCoordinator(DataUpdateCoordinator[NiuApi]):
    """Fetch each required NIU endpoint once per polling cycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: NiuApi,
        sensor_groups: set[str],
        batteries: BatteryRegistry | None = None,
        battery_store=None,
        rides: RideLog | None = None,
        rides_store=None,
        ride_mapper=None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.api = api
        self.sensor_groups = frozenset(sensor_groups)
        self._initialized = False
        self.batteries = batteries or BatteryRegistry()
        self._battery_store = battery_store
        self.rides = rides or RideLog()
        self._rides_store = rides_store
        self._ride_mapper = ride_mapper
        self._rides_changed = False
        # Extras that change slowly: (data, monotonic time of the last fetch).
        self.mileage_months: list[dict] | None = None
        self.mileage_days: list[dict] | None = None
        self.firmware: dict | None = None
        self.messages: list[dict] | None = None
        self.messages_raw: dict | None = None
        self.battery_chart: list[dict] | None = None
        self._battery_health: dict | None = None
        self._next_fetch: dict[str, float] = {}
        self._new_messages: list[dict] = []
        # When NIU last answered, and why the most recent update failed.
        self.last_success = None
        self.last_error: str | None = None
        self._skip_until = 0.0
        self._last_position: tuple[float, float] | None = None
        self._riding_until = 0.0

    @property
    def stale(self) -> bool:
        """Whether the entities show data from before a failed update."""
        return self.last_error is not None

    def _can_keep_data(self) -> bool:
        return (
            self.data is not None
            and self.last_success is not None
            and dt_util.utcnow() - self.last_success < KEEP_LAST_DATA
        )

    def _keep_data(self, reason: str) -> NiuApi:
        if self.last_error is None:
            _LOGGER.warning(
                "NIU did not answer (%s); showing the last data from %s",
                reason,
                self.last_success.isoformat() if self.last_success else "?",
            )
        else:
            _LOGGER.debug("NIU still not answering: %s", reason)
        self.last_error = reason
        return self.data

    def _sync_update_data(self) -> NiuApi:
        """Initialize once, then update each required endpoint exactly once."""
        if not self._initialized:
            _LOGGER.debug("Initializing shared NIU API client")
            self.api.initialize()
            self._initialized = True

        _LOGGER.debug(
            "Updating NIU data groups: %s", ", ".join(sorted(self.sensor_groups))
        )
        if self.sensor_groups & {SENSOR_TYPE_BAT, SENSOR_TYPE_BAT2}:
            self.api.updateBat()
        if self.sensor_groups & {
            SENSOR_TYPE_MOTO,
            SENSOR_TYPE_POS,
            SENSOR_TYPE_DIST,
        }:
            self.api.updateMoto()
        if SENSOR_TYPE_OVERALL in self.sensor_groups:
            self.api.updateMotoInfo()
        if SENSOR_TYPE_TRACK in self.sensor_groups:
            self.api.updateTrackInfo()
            self.api.updateTrackDetail()
            self._update_rides()
        self._update_extras()
        return self.api

    def _update_rides(self) -> None:
        """Add the newest rides and read a little more of the history."""
        _, changed = self.rides.merge(track_items(self.api.dataTrackInfo))
        self._rides_changed |= changed
        steps = 0
        while self.rides.backfilling and steps < BACKFILL_PAGES_PER_STEP:
            steps += 1
            try:
                tracks = self.api.get_tracks(self.rides.backfill_index, TRACK_PAGE_SIZE)
            except NiuRateLimitError:
                raise
            except NiuApiError as err:
                _LOGGER.debug("Could not read older NIU rides: %s", err)
                break
            _, changed = self.rides.merge(tracks)
            self.rides.backfill_page(tracks, TRACK_PAGE_SIZE, dt_util.utcnow())
            self._rides_changed = True
        if steps and not self.rides.backfilling:
            _LOGGER.debug("NIU ride history read: %d rides", len(self.rides.rides))

    def _due(self, name: str, interval: float) -> bool:
        now = monotonic()
        if now < self._next_fetch.get(name, 0):
            return False
        self._next_fetch[name] = now + interval
        return True

    def _update_extras(self) -> None:
        """Fetch the extras; a failure here never fails the whole update."""
        fetchers = (
            ("mileage", MILEAGE_INTERVAL, self._fetch_mileage),
            ("firmware", FIRMWARE_INTERVAL, self._fetch_firmware),
            ("messages", MESSAGES_INTERVAL, self._fetch_messages),
            ("battery health", BATTERY_HEALTH_INTERVAL, self._fetch_battery_health),
            ("battery chart", BATTERY_CHART_INTERVAL, self._fetch_battery_chart),
        )
        for name, interval, fetch in fetchers:
            if not self._due(name, interval):
                continue
            try:
                fetch()
            except NiuRateLimitError:
                raise
            except NiuApiError as err:
                _LOGGER.debug("Could not fetch NIU %s: %s", name, err)
                # Try again sooner than the normal interval.
                self._next_fetch[name] = monotonic() + min(interval, 15 * 60)

    def _fetch_mileage(self) -> None:
        self.mileage_months = self.api.get_cycling_statistics(3)
        self.mileage_days = self.api.get_cycling_statistics(1)

    def _fetch_battery_health(self) -> None:
        self._battery_health = self.api.get_battery_health()

    def _fetch_battery_chart(self) -> None:
        self.battery_chart = self.api.get_battery_chart(1)

    def _fetch_firmware(self) -> None:
        self.firmware = self.api.get_firmware()

    def _fetch_messages(self) -> None:
        raw = getattr(self.api, "get_messages_raw", None)
        if raw is not None:
            self.messages_raw = raw()
            messages = track_items(self.messages_raw)
        else:
            messages = self.api.get_messages()
        if self.messages is not None:
            seen = {message_id(message) for message in self.messages}
            self._new_messages.extend(
                message for message in messages if message_id(message) not in seen
            )
        self.messages = messages

    def _process_batteries(self, api: NiuApi) -> None:
        """Remember each battery and announce batteries seen for the first time."""
        if not self.sensor_groups & {SENSOR_TYPE_BAT, SENSOR_TYPE_BAT2}:
            return
        new_ids, changed = self.batteries.process(api.getBatteryCompartments())
        if self._battery_health is not None:
            changed |= self.batteries.process_health(self._battery_health)
            self._battery_health = None
        if changed and self._battery_store is not None:
            self._battery_store.async_delay_save(
                self.batteries.as_dict, BATTERY_SAVE_DELAY
            )
        if new_ids and self.config_entry is not None:
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_BATTERY.format(self.config_entry.entry_id),
                new_ids,
            )

    async def _async_update_data(self) -> NiuApi:
        """Fetch NIU data without blocking Home Assistant's event loop.

        A failed update keeps the last data (for up to KEEP_LAST_DATA), so a
        hiccup in NIU's cloud does not make every entity unavailable.
        """
        if monotonic() < self._skip_until and self._can_keep_data():
            return self.data
        try:
            api = await self.hass.async_add_executor_job(self._sync_update_data)
        except NiuRateLimitError as err:
            retry_after = err.retry_after or DEFAULT_RATE_LIMIT_BACKOFF
            retry_after = max(
                MIN_RATE_LIMIT_BACKOFF,
                min(retry_after, MAX_RATE_LIMIT_BACKOFF),
            )
            if self._can_keep_data():
                self._skip_until = monotonic() + retry_after
                return self._keep_data("rate limit")
            raise UpdateFailed(
                "NIU API rate limit exceeded", retry_after=retry_after
            ) from err
        except NiuApiError as err:
            if self._can_keep_data():
                return self._keep_data(str(err))
            raise UpdateFailed(f"NIU API update failed: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error while updating NIU data")
            if self._can_keep_data():
                return self._keep_data("unexpected error")
            raise UpdateFailed("Unexpected error while updating NIU data") from err
        if self.last_error is not None:
            _LOGGER.info("NIU answers again")
        self.last_error = None
        self.last_success = dt_util.utcnow()
        self._adjust_interval(api)
        self._process_batteries(api)
        await self._async_process_rides(api)
        for message in reversed(self._new_messages):
            self.hass.bus.async_fire(EVENT_NIU_MESSAGE, message_event(message))
        self._new_messages.clear()
        return api

    def _adjust_interval(self, api: NiuApi) -> None:
        """Poll faster during a ride, for a more detailed route on the map.

        NIU's ignition flag is not reliable on every scooter, so a ride is
        also recognised by the scooter's speed or by its position moving.
        """
        moving = False
        try:
            if str(api.getDataMoto("isAccOn")).strip().lower() in ("1", "true", "on"):
                moving = True
            if (_float(api.getDataMoto("nowSpeed")) or 0) > 0:
                moving = True
            lat, lng = _float(api.getDataPos("lat")), _float(api.getDataPos("lng"))
        except Exception:  # noqa: BLE001 - only a polling hint
            lat = lng = None
        if lat is not None and lng is not None and (lat, lng) != (0, 0):
            if (
                self._last_position is not None
                and distance_m(self._last_position, (lat, lng)) > MOVED_METERS
            ):
                moving = True
            self._last_position = (lat, lng)
        if moving:
            self._riding_until = monotonic() + RIDING_LINGER
        self.update_interval = (
            RIDING_POLL_INTERVAL
            if monotonic() < self._riding_until
            else POLL_INTERVAL
        )

    async def _async_process_rides(self, api: NiuApi) -> None:
        if self._ride_mapper is not None:
            try:
                self._rides_changed |= await self._ride_mapper.async_process(
                    self.rides, api
                )
            except Exception:  # noqa: BLE001 - maps are a nice-to-have
                _LOGGER.exception("Could not draw NIU ride maps")
        if self._rides_changed and self._rides_store is not None:
            self._rides_store.async_delay_save(self.rides.as_dict, RIDES_SAVE_DELAY)
        self._rides_changed = False


def message_id(message: dict) -> str:
    return str(
        message.get("msgId")
        or message.get("msgNo")
        or f"{message.get('title')}-{message.get('time')}"
    )


def message_event(message: dict) -> dict:
    """The data of a niu_message event: the fields that are known to exist."""
    time = message.get("time")
    when = None
    try:
        when = dt_util.utc_from_timestamp(float(time) / 1000).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return {
        "id": message_id(message),
        "title": message.get("title"),
        "content": message.get("content") or message.get("msgContent"),
        "main_category": message.get("mainCate"),
        "sub_category": message.get("subCate"),
        "time": when,
    }


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
