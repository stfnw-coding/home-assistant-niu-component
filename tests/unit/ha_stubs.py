"""Minimal Home Assistant stubs for unit testing the custom integration."""

from __future__ import annotations

import sys
import types


class ConfigFlow:
    """Small stand-in for Home Assistant's config-flow base class."""

    def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls.DOMAIN = domain

    async def async_set_unique_id(self, unique_id: str) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        if self.unique_id in getattr(self.hass, "configured_unique_ids", set()):
            raise AbortFlow("already_configured")

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema, errors):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }


class AbortFlow(Exception):
    """Signal that a config flow should abort."""


class DeviceInfo(dict):
    """Small stand-in for Home Assistant's DeviceInfo mapping."""

    def __init__(self, **kwargs) -> None:
        super().__init__(kwargs)


class UpdateFailed(Exception):
    """Coordinator update failure carrying an optional retry delay."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class DataUpdateCoordinator:
    """Small behavioral subset of Home Assistant's coordinator."""

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(
        self, hass, logger, *, config_entry=None, name, update_interval
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True

    async def async_config_entry_first_refresh(self) -> None:
        self.data = await self._async_update_data()


class CoordinatorEntity:
    """Small stand-in for CoordinatorEntity."""

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class SensorEntity:
    """SensorEntity stand-in."""


class SensorDeviceClass:
    BATTERY = "battery"
    ENUM = "enum"
    TIMESTAMP = "timestamp"
    DISTANCE = "distance"


class SensorStateClass:
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class TrackerEntity:
    """TrackerEntity stand-in."""


class SourceType:
    GPS = "gps"


class OptionsFlow:
    """OptionsFlow stand-in."""

    def async_create_entry(self, *, data):
        return {"type": "create_entry", "data": data}

    def async_show_form(self, *, step_id, data_schema, errors=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}


class Store:
    """In-memory storage helper stand-in."""

    def __init__(self, hass, version, key) -> None:
        self.key = key
        self.saved = None

    async def async_load(self):
        return None

    def async_delay_save(self, data_func, delay=0) -> None:
        self.saved = data_func()


class Camera:
    """Camera stand-in."""

    def __init__(self) -> None:
        self.hass = None
        self._attr_is_on = True
        self.content_type = "image/jpeg"

    @property
    def is_on(self) -> bool:
        """Mirror Home Assistant's default camera power state."""
        return self._attr_is_on


class TimeoutException(Exception):
    """httpx timeout stand-in."""


class RequestError(Exception):
    """httpx request error stand-in."""


class HTTPStatusError(Exception):
    """httpx status error stand-in."""


def install_homeassistant_stubs() -> None:
    """Install only the modules imported by the integration under test."""
    for name in tuple(sys.modules):
        if name == "homeassistant" or name.startswith("homeassistant."):
            del sys.modules[name]

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    camera = types.ModuleType("homeassistant.components.camera")
    camera.Camera = Camera
    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    device_tracker = types.ModuleType("homeassistant.components.device_tracker")
    device_tracker.TrackerEntity = TrackerEntity
    device_tracker.SourceType = SourceType

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    config_entries.ConfigFlowResult = dict
    config_entries.OptionsFlow = OptionsFlow
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda func: func
    core.ServiceCall = type("ServiceCall", (), {})
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = Store
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.sent = []
    dispatcher.async_dispatcher_send = lambda hass, signal, *args: dispatcher.sent.append(
        (signal, args)
    )
    dispatcher.async_dispatcher_connect = lambda hass, signal, target: (lambda: None)

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = DeviceInfo
    httpx_client = types.ModuleType("homeassistant.helpers.httpx_client")
    httpx_client.get_async_client = lambda hass, verify_ssl=True: None
    selector = types.ModuleType("homeassistant.helpers.selector")

    class SelectSelectorMode:
        LIST = "list"

    class TextSelectorType:
        PASSWORD = "password"

    selector.SelectSelector = lambda config: config
    selector.SelectSelectorConfig = lambda **kwargs: kwargs
    selector.SelectSelectorMode = SelectSelectorMode
    selector.TextSelector = lambda config: config
    selector.TextSelectorConfig = lambda **kwargs: kwargs
    selector.TextSelectorType = TextSelectorType
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    from datetime import datetime, timezone

    http = types.ModuleType("homeassistant.components.http")
    http.StaticPathConfig = lambda *args, **kwargs: args
    websocket_api = types.ModuleType("homeassistant.components.websocket_api")
    websocket_api.ActiveConnection = object
    websocket_api.websocket_command = lambda schema: (lambda func: func)
    websocket_api.async_register_command = lambda hass, handler: None
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
    binary_sensor.BinarySensorDeviceClass = types.SimpleNamespace(
        BATTERY_CHARGING="battery_charging", POWER="power"
    )
    const = types.ModuleType("homeassistant.const")
    const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
    const.UnitOfLength = types.SimpleNamespace(KILOMETERS="km")
    util = types.ModuleType("homeassistant.util")
    util.__path__ = []
    dt = types.ModuleType("homeassistant.util.dt")
    dt.utcnow = lambda: datetime.now(timezone.utc)
    dt.now = lambda: datetime.now().astimezone()
    dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
    util.dt = dt

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components.http": http,
        "homeassistant.components.websocket_api": websocket_api,
        "homeassistant.components.binary_sensor": binary_sensor,
        "homeassistant.const": const,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "homeassistant.components": components,
        "homeassistant.components.camera": camera,
        "homeassistant.components.sensor": sensor,
        "homeassistant.components.device_tracker": device_tracker,
        "homeassistant.helpers.storage": storage,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.httpx_client": httpx_client,
        "homeassistant.helpers.selector": selector,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }
    sys.modules.update(modules)

    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda schema: schema
    voluptuous.Required = lambda key, **kwargs: key
    voluptuous.Optional = lambda key, **kwargs: key
    voluptuous.All = lambda *validators: validators
    voluptuous.Coerce = lambda target: target
    voluptuous.Range = lambda **kwargs: kwargs
    sys.modules["voluptuous"] = voluptuous

    requests = types.ModuleType("requests")
    requests.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = requests

    httpx = types.ModuleType("httpx")
    httpx.TimeoutException = TimeoutException
    httpx.RequestError = RequestError
    httpx.HTTPStatusError = HTTPStatusError
    httpx.HTTPError = Exception
    sys.modules["httpx"] = httpx


def clear_niu_modules() -> None:
    """Remove cached integration modules so stubs are applied consistently."""
    for name in tuple(sys.modules):
        if name == "custom_components.niu" or name.startswith("custom_components.niu."):
            del sys.modules[name]
