"""Sensor entities for NIU scooters."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .battery import battery_label, compartment_is_present
from .const import (
    CONF_AUTH,
    CONF_SENSORS,
    DOMAIN,
    SENSOR_TYPE_BAT,
    SENSOR_TYPE_BAT2,
    SENSOR_TYPE_DIST,
    SENSOR_TYPE_MOTO,
    SENSOR_TYPE_OVERALL,
    SENSOR_TYPE_POS,
    SENSOR_TYPE_TRACK,
    SENSOR_TYPES,
    SIGNAL_NEW_BATTERY,
)
from .coordinator import NiuDataUpdateCoordinator
from .entity import NiuCoordinatorEntity
from .rides import summarize
from .websocket import public_ride


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up NIU sensors from a config entry."""
    coordinator: NiuDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    sensors_selected = entry.data[CONF_AUTH][CONF_SENSORS]

    entities = []
    for sensor in sensors_selected:
        if sensor == "LastTrackThumb" or sensor not in SENSOR_TYPES:
            continue
        sensor_config = SENSOR_TYPES[sensor]
        entities.append(
            NiuSensor(
                coordinator,
                sensor,
                sensor_config[0],
                sensor_config[1],
                sensor_config[2],
                sensor_config[3],
                sensor_config[4],
                sensor_config[5],
            )
        )

    entities.append(NiuInstalledBatterySensor(coordinator))
    entities.extend(
        NiuExtraSensor(coordinator, description) for description in EXTRA_SENSORS
    )
    entities.extend(
        _battery_entities(coordinator, list(coordinator.batteries.batteries))
    )
    async_add_entities(entities)

    @callback
    def _add_new_batteries(bms_ids: list[str]) -> None:
        async_add_entities(_battery_entities(coordinator, bms_ids))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_BATTERY.format(entry.entry_id), _add_new_batteries
        )
    )


def _battery_entities(coordinator, bms_ids: list[str]) -> list[SensorEntity]:
    return [
        NiuBatterySensor(coordinator, bms_id, kind)
        for bms_id in bms_ids
        for kind in BATTERY_SENSOR_KINDS
    ]


# Values that only mean something while a battery is in the compartment.
BATTERY_ONLY_FIELDS = {
    "batteryCharging",
    "chargedTimes",
    "gradeBattery",
    "temperature",
    "temperatureDesc",
    "centreCtrlBattery",
}

# kind: (unit, device class, state class, icon)
BATTERY_SENSOR_KINDS = {
    "charge": ("%", SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT, None),
    "health": ("%", None, SensorStateClass.MEASUREMENT, "mdi:battery-heart-variant"),
    "charge_cycles": (None, None, SensorStateClass.TOTAL_INCREASING, "mdi:battery-sync"),
}


class NiuSensor(NiuCoordinatorEntity, SensorEntity):
    """A NIU sensor backed by the config entry's shared coordinator."""

    def __init__(
        self,
        coordinator: NiuDataUpdateCoordinator,
        name: str,
        sensor_id: str,
        unit: str,
        data_field: str,
        sensor_group: str,
        device_class: str,
        icon: str,
    ) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        api = coordinator.data
        self._api = api
        self._data_field = data_field
        self._sensor_group = sensor_group
        self._attr_unique_id = f"sensor.niu_scooter_{api.sn}_{sensor_id}"
        self._attr_name = f"NIU Scooter {api.sensor_prefix} {name}"
        self._attr_native_unit_of_measurement = unit or None
        self._attr_device_class = device_class if device_class != "none" else None
        self._attr_icon = icon

    @property
    def native_value(self):
        """Return a value from the coordinator's cached API response."""
        if self._sensor_group in (SENSOR_TYPE_BAT, SENSOR_TYPE_BAT2):
            compartment = (
                "compartmentA" if self._sensor_group == SENSOR_TYPE_BAT else "compartmentB"
            )
            if self._data_field in BATTERY_ONLY_FIELDS and not self._battery_present(
                compartment
            ):
                return None
            if self._sensor_group == SENSOR_TYPE_BAT:
                return self._api.getDataBatA(self._data_field)
            return self._api.getDataBatB(self._data_field)
        if self._sensor_group == SENSOR_TYPE_MOTO and self._data_field == "isCharging":
            charging = self._api.is_charging()
            return None if charging is None else int(charging)
        if self._sensor_group == SENSOR_TYPE_MOTO:
            if self._data_field in BATTERY_ONLY_FIELDS and not self._battery_present():
                return None
            return self._api.getDataMoto(self._data_field)
        if self._sensor_group == SENSOR_TYPE_POS:
            return self._api.getDataPos(self._data_field)
        if self._sensor_group == SENSOR_TYPE_DIST:
            return self._api.getDataDist(self._data_field)
        if self._sensor_group == SENSOR_TYPE_OVERALL:
            return self._api.getDataOverall(self._data_field)
        if self._sensor_group == SENSOR_TYPE_TRACK:
            return self._api.getDataTrack(self._data_field)
        return None

    def _battery_present(self, compartment: str | None = None) -> bool:
        """Whether a (specific) compartment holds a battery with real data.

        Without a battery NIU reports placeholder values (0 % charge, 0 % for
        the internal battery, 100 % health), which would trigger low-battery
        automations; such sensors report "unknown" instead.
        """
        get = getattr(self._api, "getBatteryCompartments", None)
        if get is None:
            return True
        compartments = get()
        if not compartments:
            return True
        if compartment is not None:
            data = compartments.get(compartment)
            return data is None or compartment_is_present(data)
        return any(compartment_is_present(data) for data in compartments.values())

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return diagnostic attributes for the connection sensor."""
        if self._sensor_group != SENSOR_TYPE_MOTO or self._data_field != "isConnected":
            return None

        attributes = {
            "bmsId_a": self._api.getDataBatA("bmsId"),
            "latitude": self._api.getDataPos("lat"),
            "longitude": self._api.getDataPos("lng"),
            "gsm": self._api.getDataMoto("gsm"),
            "gps": self._api.getDataMoto("gps"),
            "time": self._api.getDataDist("time"),
            "range": self._api.getDataMoto("estimatedMileage"),
            "battery_a": self._api.getDataBatA("batteryCharging"),
            "battery_grade_a": self._api.getDataBatA("gradeBattery"),
            "centre_ctrl_batt": self._api.getDataMoto("centreCtrlBattery"),
        }
        if self._api.hasSecondBattery():
            attributes.update(
                {
                    "bmsId_b": self._api.getDataBatB("bmsId"),
                    "battery_b": self._api.getDataBatB("batteryCharging"),
                    "battery_grade_b": self._api.getDataBatB("gradeBattery"),
                }
            )
        return attributes


class NiuInstalledBatterySensor(NiuCoordinatorEntity, SensorEntity):
    """Which battery is in the scooter right now."""

    _attr_has_entity_name = True
    _attr_translation_key = "installed_battery"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:battery-arrow-down"

    def __init__(self, coordinator: NiuDataUpdateCoordinator) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        self._attr_unique_id = f"niu_scooter_{coordinator.data.sn}_installed_battery"

    @property
    def options(self) -> list[str]:
        labels = {battery_label(bms_id) for bms_id in self.coordinator.batteries.batteries}
        installed = self.coordinator.batteries.installed_label()
        if installed:
            labels.add(installed)
        return [*sorted(labels), "none"]

    @property
    def native_value(self) -> str:
        return self.coordinator.batteries.installed_label() or "none"

    @property
    def extra_state_attributes(self) -> dict:
        return {"bms_ids": sorted(self.coordinator.batteries.installed)}


class NiuBatterySensor(NiuCoordinatorEntity, SensorEntity):
    """Last known value of one battery, whether it is in the scooter or not."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: NiuDataUpdateCoordinator, bms_id: str, kind: str
    ) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        self._bms_id = bms_id
        self._kind = kind
        unit, device_class, state_class, icon = BATTERY_SENSOR_KINDS[kind]
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        self._attr_translation_key = f"battery_{kind}"
        self._attr_unique_id = f"niu_scooter_{coordinator.data.sn}_battery_{bms_id}_{kind}"
        label = battery_label(bms_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"battery_{bms_id}")},
            name=f"NIU battery {label}",
            translation_key="battery",
            translation_placeholders={"label": label},
            manufacturer="NIU",
            model="Battery",
            serial_number=bms_id,
        )

    @property
    def _record(self) -> dict:
        return self.coordinator.batteries.batteries.get(self._bms_id, {})

    @property
    def native_value(self):
        return self._record.get(self._kind)

    @property
    def extra_state_attributes(self) -> dict:
        record = self._record
        return {
            "bms_id": self._bms_id,
            "in_scooter": self._bms_id in self.coordinator.batteries.installed,
            "last_seen": record.get("last_seen"),
            "last_installed": record.get("last_installed"),
            "temperature": record.get("temperature"),
        }


# ---------------------------------------------------------------- extras


@dataclass(frozen=True, kw_only=True)
class NiuExtraDescription:
    key: str
    value: Callable[[NiuDataUpdateCoordinator], Any]
    attributes: Callable[[NiuDataUpdateCoordinator], dict | None] | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    category: EntityCategory | None = None
    precision: int | None = None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _this_month(months: list[dict] | None, prefix: str) -> float | None:
    if months is None:
        return None
    return round(
        sum(month["km"] for month in months if month["start"].startswith(prefix)), 1
    )


def _mileage_month(coordinator) -> float | None:
    return _this_month(coordinator.mileage_months, dt_util.now().strftime("%Y%m"))


def _mileage_year(coordinator) -> float | None:
    return _this_month(coordinator.mileage_months, dt_util.now().strftime("%Y"))


def _mileage_today(coordinator) -> float | None:
    if coordinator.mileage_days is None:
        return None
    today = dt_util.now().strftime("%Y%m%d")
    return round(
        sum(day["km"] for day in coordinator.mileage_days if day["start"] == today), 1
    )


def _months_attr(coordinator) -> dict:
    months = coordinator.mileage_months or []
    return {
        "maanden": [
            {"maand": f"{month['start'][:4]}-{month['start'][4:6]}", "km": month["km"]}
            for month in months[-36:]
        ]
    }


def _days_attr(coordinator) -> dict:
    days = coordinator.mileage_days or []
    return {
        "dagen": [
            {
                "dag": f"{day['start'][:4]}-{day['start'][4:6]}-{day['start'][6:8]}",
                "km": day["km"],
            }
            for day in days[-62:]
        ]
    }


def _rides_today(coordinator) -> int:
    return summarize(coordinator.rides.ordered(), dt_util.now())["today"]["rides"]


def _rides_attr(coordinator) -> dict:
    rides = coordinator.rides.ordered()
    summary = summarize(rides, dt_util.now())
    return {
        "km_vandaag": summary["today"]["km"],
        "km_deze_week": summary["week"]["km"],
        "ritten_deze_week": summary["week"]["rides"],
        "km_deze_maand": summary["month"]["km"],
        "ritten_deze_maand": summary["month"]["rides"],
        "ritten_in_log": len(rides),
        "geschiedenis_inlezen": coordinator.rides.backfilling,
        "recent": [public_ride(ride) for ride in rides[:5]],
    }


def _firmware_value(coordinator):
    firmware = coordinator.firmware or {}
    return firmware.get("nowVersion")


def _firmware_attr(coordinator) -> dict | None:
    firmware = coordinator.firmware
    if not firmware:
        return None
    return {
        "nieuwste_versie": firmware.get("version"),
        "update_beschikbaar": bool(_int(firmware.get("needUpdate"))),
        "update_via_app": bool(_int(firmware.get("isSupportUpdate"))),
        "release_notes": firmware.get("otaDescribe"),
    }


def _message_time(message: dict):
    stamp = _int(message.get("time"))
    if not stamp:
        return None
    try:
        return dt_util.utc_from_timestamp(stamp / 1000).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _last_message(coordinator):
    if not coordinator.messages:
        return None
    title = coordinator.messages[0].get("title")
    return str(title)[:255] if title else None


def _messages_attr(coordinator) -> dict | None:
    if not coordinator.messages:
        return None
    first = coordinator.messages[0]
    return {
        "tijd": _message_time(first),
        "main_category": first.get("mainCate"),
        "sub_category": first.get("subCate"),
        "recent": [
            {
                "titel": message.get("title"),
                "tijd": _message_time(message),
                "main_category": message.get("mainCate"),
                "sub_category": message.get("subCate"),
            }
            for message in coordinator.messages[:10]
        ],
    }


EXTRA_SENSORS: tuple[NiuExtraDescription, ...] = (
    NiuExtraDescription(
        key="last_gps_fix",
        value=lambda c: c.api.getMotoTimestamp("gpsTimestamp"),
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:crosshairs-gps",
    ),
    NiuExtraDescription(
        key="last_contact",
        value=lambda c: c.api.getMotoTimestamp("infoTimestamp"),
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:sim",
    ),
    NiuExtraDescription(
        key="gps_signal",
        value=lambda c: _int(c.api.getDataMoto("gps")),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        category=EntityCategory.DIAGNOSTIC,
    ),
    NiuExtraDescription(
        key="gsm_signal",
        value=lambda c: _int(c.api.getDataMoto("gsm")),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        category=EntityCategory.DIAGNOSTIC,
    ),
    NiuExtraDescription(
        key="rides_today",
        value=_rides_today,
        attributes=_rides_attr,
        icon="mdi:map-marker-path",
    ),
    NiuExtraDescription(
        key="mileage_today",
        value=_mileage_today,
        attributes=_days_attr,
        unit=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-today",
        precision=1,
    ),
    NiuExtraDescription(
        key="mileage_month",
        value=_mileage_month,
        attributes=_months_attr,
        unit=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-month",
        precision=1,
    ),
    NiuExtraDescription(
        key="mileage_year",
        value=_mileage_year,
        unit=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar",
        precision=0,
    ),
    NiuExtraDescription(
        key="firmware",
        value=_firmware_value,
        attributes=_firmware_attr,
        icon="mdi:chip",
        category=EntityCategory.DIAGNOSTIC,
    ),
    NiuExtraDescription(
        key="last_message",
        value=_last_message,
        attributes=_messages_attr,
        icon="mdi:message-alert-outline",
    ),
)


class NiuExtraSensor(NiuCoordinatorEntity, SensorEntity):
    """Status, ride and account data beyond the classic NIU sensors."""

    _attr_has_entity_name = True
    # Large, often-changing lists the recorder does not need to keep.
    _unrecorded_attributes = frozenset({"recent", "dagen", "maanden"})

    def __init__(
        self, coordinator: NiuDataUpdateCoordinator, description: NiuExtraDescription
    ) -> None:
        NiuCoordinatorEntity.__init__(self, coordinator)
        self._description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"niu_scooter_{coordinator.data.sn}_{description.key}"
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_entity_category = description.category
        self._attr_suggested_display_precision = description.precision
        if description.icon:
            self._attr_icon = description.icon

    @property
    def native_value(self):
        return self._description.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self._description.attributes is None:
            return None
        return self._description.attributes(self.coordinator)
