"""Set the integration up in a real Home Assistant instance with a fake NIU API."""

from __future__ import annotations

import io
from unittest.mock import patch

from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.niu.const import (
    AVAILABLE_SENSORS,
    CONF_AUTH,
    CONF_MAP_STYLE,
    CONF_PASSWORD,
    CONF_SCOOTER_ID,
    CONF_SENSORS,
    CONF_USERNAME,
    DOMAIN,
)

BATTERY = {
    "compartmentA": {
        "bmsId": "BN1GPC2B41800226",
        "isConnected": True,
        "batteryCharging": 77,
        "chargedTimes": "320",
        "gradeBattery": "68",
        "temperature": 20,
    }
}


def _initialize(self):
    self.sn = "SN123"
    self.sensor_prefix = "Niu N1s"


def _update_bat(self):
    self.dataBat = {"data": {"batteries": BATTERY}}


def _update_moto(self):
    self.dataMoto = {
        "data": {
            "postion": {"lat": 52.359718, "lng": 4.854374},
            "hdop": 1.2,
            "gps": 5,
            "gsm": 31,
            "nowSpeed": 0,
            "lastTrack": {},
        }
    }


def _update_info(self):
    self.dataMotoInfo = {"data": {"totalMileage": 20500}}


def _update_track(self):
    self.dataTrackInfo = {
        "data": [
            {
                "trackId": "T1",
                "date": "20261120",
                "startTime": 1795297129000,
                "endTime": 1795297702000,
                "distance": 5211,
                "track_thumb": "https://example.invalid/thumb.png",
            }
        ]
    }


def _detail(self, track_id, date):
    return {
        "data": {
            "trackItems": [
                {"lat": 52.359718, "lng": 4.854374, "date": 3},
                {"lat": 52.371158, "lng": 4.849582, "date": 2},
                {"lat": 52.386272, "lng": 4.869763, "date": 1},
            ]
        }
    }


def _tile():
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (240, 238, 230)).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self):
        self.content = _tile()
        self.headers = {"content-type": "image/png"}

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self):
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        return _Response()


async def test_full_setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SN123",
        data={
            CONF_AUTH: {
                CONF_USERNAME: "u@example.com",
                CONF_PASSWORD: "pw",
                CONF_SCOOTER_ID: 0,
                CONF_SENSORS: AVAILABLE_SENSORS,
            }
        },
    )
    entry.add_to_hass(hass)
    client = _Client()
    api = "custom_components.niu.api.NiuApi"
    with (
        patch(f"{api}.initialize", _initialize),
        patch(f"{api}.updateBat", _update_bat),
        patch(f"{api}.updateMoto", _update_moto),
        patch(f"{api}.updateMotoInfo", _update_info),
        patch(f"{api}.updateTrackInfo", _update_track),
        patch(f"{api}.post_track_detail", _detail),
        patch("custom_components.niu.camera.get_async_client", return_value=client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        ent_reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        by_unique = {e.unique_id: e.entity_id for e in entities}

        tracker = hass.states.get(by_unique["niu_scooter_SN123_location"])
        assert tracker.attributes["latitude"] == 52.359718
        assert tracker.attributes["longitude"] == 4.854374
        assert tracker.attributes["source_type"] == "gps"

        charge = hass.states.get(
            by_unique["niu_scooter_SN123_battery_BN1GPC2B41800226_charge"]
        )
        assert charge.state == "77"
        assert charge.attributes["in_scooter"] is True
        cycles = hass.states.get(
            by_unique["niu_scooter_SN123_battery_BN1GPC2B41800226_charge_cycles"]
        )
        assert cycles.state == "320"
        installed = hass.states.get(by_unique["niu_scooter_SN123_installed_battery"])
        assert installed.state == "0226"

        dev_reg = dr.async_get(hass)
        battery_device = dev_reg.async_get_device(
            identifiers={(DOMAIN, "battery_BN1GPC2B41800226")}
        )
        assert battery_device is not None
        print("battery device name:", battery_device.name, "| entities:", sorted(by_unique.values()))

        location_camera = by_unique["niu_scooter_SN123_location_map"]
        print("location camera:", location_camera)
        image = await async_get_image(hass, location_camera)
        assert Image.open(io.BytesIO(image.content)).size == (1024, 640)
        open("/tmp/ha_location.png", "wb").write(image.content)

        camera_id = by_unique["Niu N1s Last Track Camera"]
        image = await async_get_image(hass, camera_id)
        assert image.content_type == "image/png"
        assert Image.open(io.BytesIO(image.content)).size == (1024, 640)
        assert client.urls and all("tile.openstreetmap.org" in url for url in client.urls)
        open("/tmp/ha_route.png", "wb").write(image.content)

        # A second battery shows up after a swap: new entities appear by themselves.
        BATTERY["compartmentA"] = {
            "bmsId": "BN1GPC2B55100049",
            "isConnected": True,
            "batteryCharging": 95,
            "chargedTimes": "297",
            "gradeBattery": "70.3",
            "temperature": 18,
        }
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        by_unique = {e.unique_id: e.entity_id for e in entities}
        new_charge = hass.states.get(
            by_unique["niu_scooter_SN123_battery_BN1GPC2B55100049_charge"]
        )
        assert new_charge.state == "95"
        old_charge = hass.states.get(
            by_unique["niu_scooter_SN123_battery_BN1GPC2B41800226_charge"]
        )
        assert old_charge.state == "77"
        assert old_charge.attributes["in_scooter"] is False
        assert hass.states.get(by_unique["niu_scooter_SN123_installed_battery"]).state == "0049"

        # Options flow switches the map style and reloads.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == "form"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MAP_STYLE: "osm"}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        image = await async_get_image(hass, camera_id)
        assert any("tile.openstreetmap.org" in url for url in client.urls)
