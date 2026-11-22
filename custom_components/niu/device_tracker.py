"""GPS location of the NIU scooter."""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity

from .const import DOMAIN
from .coordinator import NiuDataUpdateCoordinator
from .entity import NiuCoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up the scooter location tracker."""
    coordinator: NiuDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NiuLocationTracker(coordinator)])


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


class NiuLocationTracker(NiuCoordinatorEntity, TrackerEntity):
    """Where the scooter is, as last reported by NIU."""

    _attr_has_entity_name = True
    _attr_translation_key = "location"
    _attr_icon = "mdi:moped-electric"

    def __init__(self, coordinator: NiuDataUpdateCoordinator) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        self._api = coordinator.data
        self._attr_unique_id = f"niu_scooter_{self._api.sn}_location"

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        latitude = _float(self._api.getDataPos("lat"))
        longitude = _float(self._api.getDataPos("lng"))
        if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
            return None
        return latitude

    @property
    def longitude(self) -> float | None:
        if self.latitude is None:
            return None
        return _float(self._api.getDataPos("lng"))

    @property
    def location_accuracy(self) -> float:
        # NIU reports HDOP; roughly 5 m per HDOP unit is a sane GPS estimate.
        hdop = _float(self._api.getDataMoto("hdop"))
        if not hdop:
            return 0
        return round(hdop * 5, 1)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "gps_signal": self._api.getDataMoto("gps"),
            "gsm_signal": self._api.getDataMoto("gsm"),
            "speed": self._api.getDataMoto("nowSpeed"),
        }
