"""Binary sensors for NIU scooters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .api import NiuApi
from .const import DOMAIN
from .coordinator import NiuDataUpdateCoordinator
from .entity import NiuCoordinatorEntity


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "on", "yes"):
            return True
        if lowered in ("false", "off", "no"):
            return False
        if lowered == "":
            return None  # not reported by this scooter (e.g. N1S 2019)
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, kw_only=True)
class NiuBinaryDescription:
    key: str
    value: Callable[[NiuApi], bool | None]
    device_class: BinarySensorDeviceClass | None = None
    icon: str | None = None


BINARY_SENSORS = (
    NiuBinaryDescription(
        key="charging",
        value=lambda api: api.is_charging(),
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    NiuBinaryDescription(
        key="ignition",
        value=lambda api: _flag(api.getDataMoto("isAccOn")),
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:key-variant",
    ),
    NiuBinaryDescription(
        key="alarm_armed",
        value=lambda api: _flag(api.getDataMoto("isFortificationOn")),
        icon="mdi:shield-moped",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator: NiuDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NiuBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class NiuBinarySensor(NiuCoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: NiuDataUpdateCoordinator, description: NiuBinaryDescription
    ) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        self._description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"niu_scooter_{coordinator.data.sn}_{description.key}"
        self._attr_device_class = description.device_class
        if description.icon:
            self._attr_icon = description.icon

    @property
    def is_on(self) -> bool | None:
        return self._description.value(self.coordinator.data)
