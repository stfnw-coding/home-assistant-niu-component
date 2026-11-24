"""Unit tests for the ride log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[2] / "custom_components" / "niu"


def _load():
    package = types.ModuleType("niu_rides_test")
    package.__path__ = [str(ROOT)]
    sys.modules["niu_rides_test"] = package
    spec = importlib.util.spec_from_file_location("niu_rides_test.rides", ROOT / "rides.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rides = _load()

NOW = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)


def track(track_id: str, start: datetime, **extra) -> dict:
    ms = int(start.timestamp() * 1000)
    return {
        "trackId": track_id,
        "startTime": ms,
        "endTime": ms + 87_000,
        "ridingtime": 87,
        "distance": 401,
        "avespeed": 17.63,
        "power_consumption": "1",
        "date": start.strftime("%Y%m%d"),
        "startPoint": {"lat": "52.359619", "lng": "4.854345"},
        "lastPoint": {"lat": 52.3701, "lng": 4.8601},
        "track_thumb": "https://example.invalid/t.png",
        **extra,
    }


class RideLogTest(unittest.TestCase):
    def test_track_becomes_a_ride(self) -> None:
        ride = rides.ride_from_track(track("T1", NOW))
        self.assertEqual(ride["id"], "T1")
        self.assertEqual(ride["start"], NOW.isoformat())
        self.assertEqual(ride["duration_s"], 87)
        self.assertEqual(ride["distance_m"], 401)
        self.assertEqual(ride["consumption"], 1.0)
        self.assertEqual(ride["start_point"], [52.359619, 4.854345])

    def test_zero_points_are_unknown(self) -> None:
        ride = rides.ride_from_track(track("T1", NOW, startPoint={"lat": 0, "lng": 0}))
        self.assertIsNone(ride["start_point"])

    def test_merge_adds_new_and_keeps_worked_out_fields(self) -> None:
        log = rides.RideLog()
        new, changed = log.merge([track("T1", NOW)])
        self.assertEqual((new, changed), (["T1"], True))
        log.rides["T1"].update({"map": "/x.jpg", "from": "Thuis"})
        new, changed = log.merge([track("T1", NOW)])
        self.assertEqual((new, changed), ([], False))
        self.assertEqual(log.rides["T1"]["map"], "/x.jpg")
        # A changed route means the map must be drawn again.
        log.merge([track("T1", NOW, track_thumb="https://example.invalid/new.png")])
        self.assertNotIn("map", log.rides["T1"])
        self.assertEqual(log.rides["T1"]["from"], "Thuis")

    def test_storage_round_trip_keeps_order_and_salt(self) -> None:
        log = rides.RideLog()
        log.merge([track("OLD", NOW - timedelta(days=1)), track("NEW", NOW)])
        restored = rides.RideLog(log.as_dict())
        self.assertEqual([r["id"] for r in restored.ordered()], ["NEW", "OLD"])
        self.assertEqual(restored.map_filename("NEW"), log.map_filename("NEW"))
        self.assertNotIn("NEW", log.map_filename("NEW"))

    def test_backfill_continues_past_a_short_page(self) -> None:
        log = rides.RideLog()
        self.assertTrue(log.backfilling)
        log.backfill_page([track(str(i), NOW) for i in range(5)], 5, NOW)
        self.assertEqual(log.backfill_index, 1)
        log.backfill_page([track("x", NOW)], 5, NOW)
        self.assertTrue(log.backfilling)
        self.assertEqual([entry["count"] for entry in log.backfill_trace], [5, 1])

    def test_backfill_stops_after_two_empty_pages(self) -> None:
        log = rides.RideLog()
        log.backfill_page([], 5, NOW)
        self.assertTrue(log.backfilling)
        log.backfill_page([], 5, NOW)
        self.assertFalse(log.backfilling)

    def test_backfill_stops_when_a_whole_page_is_too_old(self) -> None:
        log = rides.RideLog()
        old = NOW - timedelta(days=rides.BACKFILL_DAYS + 1)
        log.backfill_page([track("new", NOW), track("old", old)], 5, NOW)
        self.assertTrue(log.backfilling)
        log.backfill_page([track(str(i), old) for i in range(5)], 5, NOW)
        self.assertFalse(log.backfilling)

    def test_logs_paged_by_offset_read_the_history_again(self) -> None:
        restored = rides.RideLog({"rides": [], "salt": "x", "backfill_index": None})
        self.assertTrue(restored.backfilling)
        log = rides.RideLog()
        log.backfill_page([], 5, NOW)
        log.backfill_page([], 5, NOW)
        self.assertFalse(rides.RideLog(log.as_dict()).backfilling)

    def test_restart_backfill(self) -> None:
        log = rides.RideLog()
        log.backfill_page([], 5, NOW)
        log.backfill_page([], 5, NOW)
        log.restart_backfill()
        self.assertEqual((log.backfill_index, log.backfill_trace), (0, []))

    def test_finished_backfill_survives_a_restart(self) -> None:
        log = rides.RideLog()
        log.backfill_page([], 5, NOW)
        log.backfill_page([], 5, NOW)
        self.assertFalse(rides.RideLog(log.as_dict()).backfilling)

    def test_log_is_capped(self) -> None:
        log = rides.RideLog()
        log.merge(
            [track(str(i), NOW - timedelta(hours=i)) for i in range(rides.MAX_RIDES + 3)]
        )
        self.assertEqual(len(log.rides), rides.MAX_RIDES)
        self.assertIn("0", log.rides)

    def test_summary(self) -> None:
        local = timezone(timedelta(hours=1))
        now = datetime(2026, 11, 23, 21, 0, tzinfo=local)  # a Monday
        ride_list = [
            rides.ride_from_track(track("a", now - timedelta(hours=1))),
            rides.ride_from_track(track("b", now - timedelta(days=1))),  # Sunday
            rides.ride_from_track(track("c", now - timedelta(days=30))),
        ]
        summary = rides.summarize(ride_list, now)
        self.assertEqual(summary["today"], {"rides": 1, "km": 0.4})
        self.assertEqual(summary["week"], {"rides": 1, "km": 0.4})
        self.assertEqual(summary["month"]["rides"], 2)


class PlaceTest(unittest.TestCase):
    def test_zone_wins_and_smallest_zone_is_used(self) -> None:
        zones = [
            {"name": "Amsterdam", "latitude": 52.37, "longitude": 4.89, "radius": 8000},
            {"name": "Thuis", "latitude": 52.359739, "longitude": 4.8542905, "radius": 100},
        ]
        self.assertEqual(rides.zone_name([52.359619, 4.854345], zones), "Thuis")
        self.assertEqual(rides.zone_name([52.3701, 4.8601], zones), "Amsterdam")
        self.assertIsNone(rides.zone_name([51.0, 4.0], zones))

    def test_address_label(self) -> None:
        self.assertEqual(
            rides.address_label(
                {"address": {"road": "Overtoom", "house_number": "12", "city": "Amsterdam"}}
            ),
            "Overtoom 12, Amsterdam",
        )
        self.assertEqual(
            rides.address_label({"address": {"suburb": "Oud-West", "city": "Amsterdam"}}),
            "Oud-West, Amsterdam",
        )
        self.assertIsNone(rides.address_label("nope"))


class RouteChoiceTest(unittest.TestCase):
    """NIU sometimes keeps only the first part of a ride."""

    # 24 Nov 2026: 6.76 km to Amsterdam-Noord; NIU kept the first ~1 km.
    TRAIL = [
        (52.359683, 4.854649), (52.364509, 4.857871), (52.367172, 4.872077),
        (52.371623, 4.882956), (52.380947, 4.888946), (52.380443, 4.899354),
        (52.381103, 4.900853), (52.382114, 4.903091), (52.386703, 4.905975),
        (52.389347, 4.903252),
    ]

    def ride(self, end):
        return {
            "distance_m": 6763,
            "start_point": [52.359683, 4.854649],
            "end_point": list(end),
        }

    def test_incomplete_niu_route_uses_the_trail(self) -> None:
        niu_points = [(52.359683, 4.854649), (52.3630, 4.8560), (52.3665, 4.8574)]
        source, route = rides.choose_route(
            self.ride((52.3665, 4.8574)), niu_points, None, self.TRAIL
        )
        self.assertEqual(source, "ha")
        self.assertEqual(route[-1], [52.389347, 4.903252])
        self.assertEqual(route[0], [52.359683, 4.854649])

    def test_complete_niu_route_is_kept(self) -> None:
        source, route = rides.choose_route(
            self.ride((52.389347, 4.903252)), list(self.TRAIL), None, self.TRAIL
        )
        self.assertEqual((source, route), ("niu", None))

    def test_without_trail_niu_is_kept(self) -> None:
        source, _ = rides.choose_route(self.ride((52.3665, 4.8574)), None, None, [])
        self.assertEqual(source, "niu")

    def test_corrected_end_point_survives_a_refresh(self) -> None:
        log = rides.RideLog()
        log.merge([track("T1", NOW)])
        log.rides["T1"].update(
            {"end_point": [52.389347, 4.903252], "end_corrected": True, "map": "/m.jpg"}
        )
        log.merge([track("T1", NOW)])
        self.assertEqual(log.rides["T1"]["end_point"], [52.389347, 4.903252])
        self.assertEqual(log.rides["T1"]["map"], "/m.jpg")

    def test_recent_unchecked_rides_need_a_check(self) -> None:
        log = rides.RideLog()
        log.merge([track("NEW", NOW), track("OLD", NOW - timedelta(days=30))])
        for ride in log.rides.values():
            ride["map"] = "/m.jpg"
        self.assertEqual([r["id"] for r in log.needing_map(NOW)], ["NEW"])
        log.rides["NEW"]["route_checked"] = True
        self.assertEqual(log.needing_map(NOW), [])
