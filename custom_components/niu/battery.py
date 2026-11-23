"""Remember every battery that has been in the scooter.

Scooters with a single compartment report only the battery that is in it right
now. People who swap between batteries lose the charge, health and cycle count
of the one on the shelf. This registry keeps the last known values of each
battery, keyed by its BMS id, so every battery gets its own entities.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

STORAGE_VERSION = 1


def battery_label(bms_id: str) -> str:
    """Return a short, stable name for a battery: the last 4 characters."""
    return bms_id[-4:]


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def compartment_is_present(compartment: dict[str, Any]) -> bool:
    """Return whether a compartment holds a battery that reports real data.

    When the battery is taken out NIU keeps reporting the old BMS id with
    placeholder values, such as 0 % charge, 0 cycles or 100 % health. A
    battery in use never reports 0 % (the BMS shuts down before that), so a
    zero charge is treated as an empty compartment.
    """
    if not compartment.get("bmsId"):
        return False
    connected = compartment.get("isConnected")
    if connected is False or str(connected).lower() in ("false", "0"):
        return False
    charge = _number(compartment.get("batteryCharging"))
    cycles = _number(compartment.get("chargedTimes"))
    if not charge or not cycles:
        return False
    return True


class BatteryRegistry:
    """Last known state of every battery seen by this scooter."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.batteries: dict[str, dict[str, Any]] = dict(
            (data or {}).get("batteries", {})
        )
        self.installed: dict[str, str] = {}

    def as_dict(self) -> dict[str, Any]:
        return {"batteries": self.batteries}

    def process(
        self,
        compartments: dict[str, dict[str, Any]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> tuple[list[str], bool]:
        """Update from the battery endpoint.

        Returns the BMS ids seen for the first time and whether anything
        changed that should be saved.
        """
        new_ids: list[str] = []
        changed = False
        installed: dict[str, str] = {}
        timestamp = now().isoformat()

        for name, compartment in sorted(compartments.items()):
            if not compartment_is_present(compartment):
                continue
            bms_id = str(compartment["bmsId"])
            installed[bms_id] = name
            record = self.batteries.get(bms_id)
            if record is None:
                record = {"first_seen": timestamp}
                self.batteries[bms_id] = record
                new_ids.append(bms_id)

            values = {
                "charge": _number(compartment.get("batteryCharging")),
                "health": _number(compartment.get("gradeBattery")),
                "charge_cycles": _number(compartment.get("chargedTimes")),
                "temperature": _number(compartment.get("temperature")),
            }
            for key, value in values.items():
                if value is not None and record.get(key) != value:
                    record[key] = value
                    changed = True
            if record.get("compartment") != name:
                record["compartment"] = name
                changed = True
            # Kept current in memory; written to disk along with real changes.
            record["last_seen"] = timestamp

        if installed != self.installed:
            for bms_id in installed.keys() - self.installed.keys():
                self.batteries[bms_id]["last_installed"] = timestamp
            self.installed = installed
            changed = True

        return new_ids, changed or bool(new_ids)

    def process_health(self, compartments: dict[str, dict[str, Any]]) -> bool:
        """Keep NIU's health report (grade history, faults) per battery."""
        changed = False
        for compartment in compartments.values():
            bms_id = compartment.get("bmsId")
            if not bms_id or str(bms_id) not in self.batteries:
                continue
            record = self.batteries[str(bms_id)]
            history = []
            for item in compartment.get("healthRecords") or []:
                if not isinstance(item, dict):
                    continue
                history.append(
                    {
                        "time": _number(item.get("time")),
                        "name": item.get("name"),
                        "lost": _number(item.get("result")),
                        "cycles": _number(item.get("chargeCount")),
                    }
                )
            history.sort(key=lambda entry: entry["time"] or 0)
            faults = [
                fault for fault in compartment.get("faults") or [] if isinstance(fault, dict)
            ]
            for key, value in (("health_history", history), ("faults", faults)):
                if record.get(key) != value:
                    record[key] = value
                    changed = True
        return changed

    def installed_label(self) -> str | None:
        if not self.installed:
            return None
        return ", ".join(sorted(battery_label(bms_id) for bms_id in self.installed))


# More than this per kilometre is not riding but a battery swap.
MAX_PCT_PER_KM = 8
MIN_RUN_STEPS = 3


def range_per_full_charge(points: list[dict]) -> float | None:
    """Kilometres per 100 % of charge, from NIU's battery chart.

    NIU samples the charge about once per kilometre for whichever battery is
    in the compartment, and mixes in stale readings, so the chart jumps
    around with swaps and charges. Only clean stretches count: at least
    ``MIN_RUN_STEPS`` steps in a row of one to three kilometres, each using
    a plausible amount of charge.
    """
    distance = used = 0.0
    run_km = run_pct = 0.0
    run_steps = 0

    def close_run() -> None:
        nonlocal distance, used, run_km, run_pct, run_steps
        if run_steps >= MIN_RUN_STEPS:
            distance += run_km
            used += run_pct
        run_km = run_pct = 0.0
        run_steps = 0

    for first, second in zip(points, points[1:]):
        km = second["km"] - first["km"]
        pct = first["pct"] - second["pct"]
        if 0 < km <= 3 and 0 < pct <= MAX_PCT_PER_KM * km:
            run_km += km
            run_pct += pct
            run_steps += 1
        else:
            close_run()
    close_run()
    if used < 10:
        return None
    return round(distance / used * 100, 1)
