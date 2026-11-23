"""Tests for the route map, track details and the battery registry."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.unit.ha_stubs import clear_niu_modules, install_homeassistant_stubs

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

RIDE = [
    (52.386272, 4.869763),
    (52.385276, 4.862635),
    (52.379932, 4.853345),
    (52.371158, 4.849582),
    (52.360172, 4.853991),
]

PRESENT = {
    "bmsId": "BN1GPC2B55100049",
    "isConnected": True,
    "batteryCharging": 84,
    "chargedTimes": "296",
    "gradeBattery": "70.4",
    "temperature": 18,
}
REMOVED = {
    "bmsId": "BN1GPC2B55100049",
    "isConnected": True,
    "batteryCharging": 0,
    "chargedTimes": "0",
    "gradeBattery": "100",
    "temperature": 15,
}
OTHER = {
    "bmsId": "BN1GPC2B41800226",
    "isConnected": True,
    "batteryCharging": 77,
    "chargedTimes": "320",
    "gradeBattery": "68",
    "temperature": 20,
}


def _now():
    return datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


class TrackDetailTest(unittest.TestCase):
    """Parsing and fetching the GPS points of a ride."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    def setUp(self) -> None:
        install_homeassistant_stubs()
        clear_niu_modules()
        self.api_module = importlib.import_module("custom_components.niu.api")

    def test_points_are_put_in_ride_order_by_timestamp(self) -> None:
        payload = {
            "data": {
                "trackItems": [
                    {"lat": "52.36", "lng": "4.85", "date": 3000},
                    {"lat": 52.38, "lng": 4.86, "date": 1000},
                    {"lat": 0, "lng": 0, "date": 1500},
                    {"lat": "x", "lng": 4.0, "date": 1700},
                    {"lat": 52.37, "lng": 4.85, "date": 2000},
                ]
            }
        }
        self.assertEqual(
            self.api_module.parse_track_points(payload),
            [(52.38, 4.86), (52.37, 4.85), (52.36, 4.85)],
        )

    def test_points_without_timestamps_are_reversed(self) -> None:
        payload = {"data": {"trackItems": [{"lat": 2, "lng": 2}, {"lat": 1, "lng": 1}]}}
        self.assertEqual(
            self.api_module.parse_track_points(payload), [(1.0, 1.0), (2.0, 2.0)]
        )

    def test_points_are_found_under_other_keys(self) -> None:
        payload = {
            "data": {
                "track": {
                    "points": [
                        {"latitude": "52.1", "longitude": "4.1", "time": 2},
                        {"latitude": "52.0", "longitude": "4.0", "time": 1},
                    ]
                }
            }
        }
        self.assertEqual(
            self.api_module.parse_track_points(payload), [(52.0, 4.0), (52.1, 4.1)]
        )
        self.assertIn("points", self.api_module.describe_shape(payload))

    def test_bad_payloads_give_no_points(self) -> None:
        for payload in (None, {}, {"data": None}, {"data": {"trackItems": "x"}}):
            self.assertEqual(self.api_module.parse_track_points(payload), [])

    def _api_with_track(self, track_id="T1"):
        api = self.api_module.NiuApi("user", "pw", 0)
        api.sn = "SN"
        api.dataTrackInfo = {"data": [{"trackId": track_id, "date": "20261120"}]}
        return api

    def test_details_are_fetched_once_per_track(self) -> None:
        api = self._api_with_track()
        payload = {"data": {"trackItems": [{"lat": 1, "lng": 1}, {"lat": 2, "lng": 2}]}}
        with patch.object(api, "post_track_detail", return_value=payload) as post:
            api.updateTrackDetail()
            api.updateTrackDetail()
        post.assert_called_once_with("T1", "20261120")
        self.assertEqual(api.track_points_id, "T1")
        self.assertEqual(len(api.track_points), 2)

    def test_failed_details_are_retried_later_not_every_poll(self) -> None:
        api = self._api_with_track()
        error = self.api_module.NiuResponseError("nope")
        with patch.object(api, "post_track_detail", side_effect=error) as post:
            api.updateTrackDetail()
            api.updateTrackDetail()
        self.assertEqual(post.call_count, 1)
        self.assertIsNone(api.track_points_id)

    def test_rate_limit_still_reaches_the_coordinator(self) -> None:
        api = self._api_with_track()
        error = self.api_module.NiuRateLimitError(60)
        with patch.object(api, "post_track_detail", side_effect=error):
            with self.assertRaises(self.api_module.NiuRateLimitError):
                api.updateTrackDetail()


class MapRenderTest(unittest.TestCase):
    """Viewport maths and image rendering."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    def setUp(self) -> None:
        install_homeassistant_stubs()
        clear_niu_modules()
        self.render = importlib.import_module("custom_components.niu.map_render")

    def test_viewport_fits_the_whole_ride(self) -> None:
        viewport = self.render.fit_viewport(RIDE, 512)
        for lat, lng in RIDE:
            x, y = viewport.to_image(lat, lng)
            self.assertTrue(
                self.render.PADDING - 1 <= x <= viewport.width - self.render.PADDING + 1
            )
            self.assertTrue(
                self.render.PADDING - 1 <= y <= viewport.height - self.render.PADDING + 1
            )
        # One more zoom level would no longer fit.
        self.assertLess(viewport.zoom, self.render.MAX_ZOOM)

    def test_every_tile_lands_inside_the_image(self) -> None:
        viewport = self.render.fit_viewport(RIDE, 256)
        for _, x, y in viewport.tiles():
            left, top = viewport.tile_offset(x, y)
            self.assertGreater(left + 256, 0)
            self.assertLess(left, viewport.width)
            self.assertGreater(top + 256, 0)
            self.assertLess(top, viewport.height)

    def test_tile_url(self) -> None:
        style = self.render.TILE_STYLES["osm"]
        self.assertEqual(
            self.render.tile_url(style, 13, 4206, 2690),
            "https://tile.openstreetmap.org/13/4206/2690.png",
        )

    def test_render_produces_png(self) -> None:
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow ships with Home Assistant
            self.skipTest("Pillow not installed")
        style = self.render.TILE_STYLES["dark"]
        viewport = self.render.fit_viewport(RIDE, style.tile_size)
        tile = io.BytesIO()
        Image.new("RGB", (style.tile_size, style.tile_size), (240, 240, 230)).save(tile, "PNG")
        tiles = {key: tile.getvalue() for key in viewport.tiles()}
        tiles[viewport.tiles()[0]] = b"broken"
        image = Image.open(
            io.BytesIO(self.render.render_route(RIDE, viewport, style, tiles))
        )
        self.assertEqual(image.size, (viewport.width, viewport.height))


class BatteryRegistryTest(unittest.TestCase):
    """Swapping batteries keeps each battery's own values."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    def setUp(self) -> None:
        install_homeassistant_stubs()
        clear_niu_modules()
        self.battery = importlib.import_module("custom_components.niu.battery")

    def test_swap_keeps_both_batteries(self) -> None:
        registry = self.battery.BatteryRegistry()
        new_ids, changed = registry.process({"compartmentA": PRESENT}, _now)
        self.assertEqual(new_ids, ["BN1GPC2B55100049"])
        self.assertTrue(changed)
        self.assertEqual(registry.installed_label(), "0049")

        # Battery taken out: placeholder values must not overwrite the real ones.
        new_ids, changed = registry.process({"compartmentA": REMOVED}, _now)
        self.assertEqual(new_ids, [])
        self.assertTrue(changed)
        self.assertIsNone(registry.installed_label())
        record = registry.batteries["BN1GPC2B55100049"]
        self.assertEqual(record["charge"], 84)
        self.assertEqual(record["charge_cycles"], 296)
        self.assertEqual(record["health"], 70.4)

        new_ids, _ = registry.process({"compartmentA": OTHER}, _now)
        self.assertEqual(new_ids, ["BN1GPC2B41800226"])
        self.assertEqual(registry.installed_label(), "0226")
        self.assertEqual(len(registry.batteries), 2)
        self.assertEqual(registry.batteries["BN1GPC2B55100049"]["charge"], 84)

    def test_unchanged_poll_does_not_ask_for_a_save(self) -> None:
        registry = self.battery.BatteryRegistry()
        registry.process({"compartmentA": PRESENT}, _now)
        _, changed = registry.process({"compartmentA": PRESENT}, _now)
        self.assertFalse(changed)

    def test_restores_from_storage(self) -> None:
        registry = self.battery.BatteryRegistry()
        registry.process({"compartmentA": PRESENT}, _now)
        restored = self.battery.BatteryRegistry(registry.as_dict())
        self.assertIn("BN1GPC2B55100049", restored.batteries)
        new_ids, _ = restored.process({"compartmentA": PRESENT}, _now)
        self.assertEqual(new_ids, [])

    def test_zero_charge_with_old_cycle_count_counts_as_removed(self) -> None:
        compartment = {**OTHER, "batteryCharging": 0}
        self.assertFalse(self.battery.compartment_is_present(compartment))

    def test_disconnected_compartment_is_ignored(self) -> None:
        compartment = {**PRESENT, "isConnected": False}
        self.assertFalse(self.battery.compartment_is_present(compartment))
        self.assertFalse(self.battery.compartment_is_present({**PRESENT, "bmsId": ""}))


if __name__ == "__main__":
    unittest.main()


class ThumbnailTraceTest(unittest.TestCase):
    """Redraw the route from a synthetic NIU-style thumbnail."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    def setUp(self) -> None:
        install_homeassistant_stubs()
        clear_niu_modules()
        self.trace = importlib.import_module("custom_components.niu.thumb_route")
        self.render = importlib.import_module("custom_components.niu.map_render")

    def _thumbnail(self, start_px, end_px):
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover
            self.skipTest("Pillow not installed")
        image = Image.new("RGB", (400, 400), (251, 248, 241))
        draw = ImageDraw.Draw(image)
        middle = (start_px[0] - 60, (start_px[1] + end_px[1]) / 2)
        draw.line([start_px, middle, end_px], fill=(66, 107, 242), width=8)
        # Start pin head above its tip, end pin with its tip on the end point.
        sx, sy = start_px
        draw.ellipse((sx - 14, sy - 40, sx + 14, sy - 12), fill=(110, 155, 248))
        ex, ey = end_px
        draw.polygon([(ex - 14, ey - 30), (ex + 14, ey - 30), (ex, ey)], fill=(223, 24, 43))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def test_route_lands_between_the_ride_ends(self) -> None:
        start, end = (52.401, 4.934), (52.385, 4.910)
        # Place the pins where a north-up map of this ride would put them.
        sm = complex(*self.render.project(*start, 0, 256))
        em = complex(*self.render.project(*end, 0, 256))
        scale = 250 / abs(em - sm)
        start_px = (300, 60)
        end_px = (
            300 + (em - sm).real * scale,
            60 + (em - sm).imag * scale,
        )
        segments = self.trace.route_from_thumbnail(
            self._thumbnail(start_px, end_px), start, end
        )
        self.assertTrue(segments)
        points = [p for segment in segments for p in segment]
        nearest_end = min(
            abs(lat - end[0]) + abs(lng - end[1]) for lat, lng in points
        )
        self.assertLess(nearest_end, 0.001)
        for lat, lng in points:
            self.assertTrue(52.38 < lat < 52.41 and 4.90 < lng < 4.94)

    def test_rotated_pins_are_rejected(self) -> None:
        # Pins left/right while the coordinates run north/south.
        segments = self.trace.route_from_thumbnail(
            self._thumbnail((320, 200), (80, 210)), (52.40, 4.92), (52.38, 4.92)
        )
        self.assertIsNone(segments)


class BatteryHealthTest(unittest.TestCase):
    """NIU's health report and the range estimate."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    def setUp(self) -> None:
        install_homeassistant_stubs()
        clear_niu_modules()

    def test_health_history_is_kept_per_battery(self) -> None:
        battery = importlib.import_module("custom_components.niu.battery")
        registry = battery.BatteryRegistry({"batteries": {"BMS1": {"health": 70}}})
        report = {
            "compartmentA": {
                "bmsId": "BMS1",
                "faults": [{"code": 1}],
                "healthRecords": [
                    {"result": "3", "chargeCount": "250", "time": 20, "name": "b"},
                    {"result": "1", "chargeCount": "100", "time": 10, "name": "a"},
                ],
            },
            "compartmentB": {"bmsId": "UNKNOWN", "healthRecords": []},
        }
        self.assertTrue(registry.process_health(report))
        record = registry.batteries["BMS1"]
        self.assertEqual([entry["cycles"] for entry in record["health_history"]], [100, 250])
        self.assertEqual(record["faults"], [{"code": 1}])
        self.assertNotIn("UNKNOWN", registry.batteries)
        self.assertFalse(registry.process_health(report))

    def test_range_counts_only_discharge_steps(self) -> None:
        battery = importlib.import_module("custom_components.niu.battery")
        points = [
            {"km": 100.0, "pct": 90.0},
            {"km": 115.0, "pct": 40.0},
            {"km": 115.0, "pct": 95.0},
            {"km": 121.0, "pct": 75.0},
        ]
        self.assertIsNone(battery.range_per_full_charge(points))
        # NIU's real chart (23 Nov 2026): stale 91 % readings and swaps in
        # between; only the clean stretches count.
        pct = [74,71,89,91,91,54,50,47,44,89,89,89,91,89,26,91,89,18,91,91,78,91,91,72,
               91,91,91,91,91,91,82,91,91,91,91,70,91,91,91,91,54,91,48,91,91,91,91,91,
               30,91,26,91,21,91,91,77,91,73,71,91,65,63,60,91,91,91,47,91,91,40,91,91,
               91,91,91,91,65,62,58,91,51,91,46,33,30,27,90,86,83,81,78,76,74,72,69,69,65]
        real = [{"km": 20438.0 + i, "pct": float(p)} for i, p in enumerate(pct)]
        self.assertEqual(battery.range_per_full_charge(real), 35.5)
