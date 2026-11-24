"""Calculate annual efficiency from the Remocon web metering report."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite
from sys import float_info
from typing import Any, Final

_TABS: Final = ("ProducedEnergy", "ConsumedElectricity")
_USES: Final = ("Heating", "Dhw")
_WEB_MONTHS: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


@dataclass(frozen=True, slots=True)
class GalevoAnnualMetering:
    """Estimated performance factor for one completed calendar year."""

    year: int
    performance_factor: float


def _first_report_month(value: Any) -> tuple[int, int] | None:
    """Read the oldest month in either ISO or English web-session format."""
    if not isinstance(value, str):
        return None
    label = value.strip().casefold()
    if match := re.fullmatch(r"([0-9]{4})-(0[1-9]|1[0-2])", label):
        return int(match[1]), int(match[2])
    if match := re.fullmatch(r"([a-z]+)\s+([0-9]{4})", label):
        for number, name in enumerate(_WEB_MONTHS, start=1):
            if match[1] in (name, name[:3]):
                return int(match[2]), number
    return None


def _monthly_values(items: Any) -> dict[str, float] | None:
    """Read all twelve distinct monthly buckets of a PreviousYear series."""
    if not isinstance(items, list) or len(items) != 12:
        return None
    months: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        label = item.get("x")
        value = item.get("y")
        if not isinstance(label, str) or not (label := label.strip().casefold()) or label in months:
            return None
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not 0 <= value <= float_info.max
        ):
            return None
        months[label] = float(value)
    return months


def galevo_annual_metering(payload: Any, *, year: int) -> GalevoAnnualMetering | None:
    """Return the previous calendar year's factor if all four series cover it."""
    if not isinstance(payload, dict) or payload.get("isIntensiveLogsActive") is True:
        return None
    # The chart can contain zero-filled buckets from before the first recorded month.
    oldest = _first_report_month(payload.get("oldestMonth"))
    if oldest is None or not (1, 1) <= oldest <= (year, 1):
        return None
    raw = payload.get("asKwhRaw")
    histogram = raw.get("histogramData") if isinstance(raw, dict) else None
    if not isinstance(histogram, list):
        return None

    rows: dict[tuple[str, str], dict[str, float]] = {}
    for row in histogram:
        if not isinstance(row, dict) or row.get("period") != "PreviousYear":
            continue
        tab, use = row.get("tab"), row.get("series")
        if tab not in _TABS or use not in _USES:
            continue
        key = (tab, use)
        values = _monthly_values(row.get("items"))
        if key in rows or values is None:
            return None
        rows[key] = values

    if len(rows) != len(_TABS) * len(_USES):
        return None
    months = rows["ProducedEnergy", "Heating"].keys()
    if any(values.keys() != months for values in rows.values()):
        return None
    produced, consumed = (
        sum(value for use in _USES for value in rows[tab, use].values()) for tab in _TABS
    )
    if consumed <= 0 or not isfinite(consumed):
        return None
    factor = produced / consumed
    return GalevoAnnualMetering(year, factor) if isfinite(factor) else None
