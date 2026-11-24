"""Exercise previous-year web metering with synthetic chart data."""

import json
from pathlib import Path

import pytest

from custom_components.elco_aerotop.metering import galevo_annual_metering


@pytest.fixture
def report():
    path = Path(__file__).parent / "fixtures" / "galevo_metering.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_combines_energy_before_dividing_and_ignores_other_rows(report):
    rows = report["asKwhRaw"]["histogramData"]
    rows[2]["items"].reverse()
    rows.extend(
        [
            {**rows[0], "period": "CurrentYear", "items": []},
            {**rows[1], "series": "Cooling", "items": []},
            {**rows[1], "series": "RoomTemp", "items": []},
        ]
    )
    result = galevo_annual_metering(report, year=2025)
    assert result is not None
    assert result.year == 2025
    assert result.performance_factor == pytest.approx((960 + 360) / (240 + 180))


@pytest.mark.parametrize("oldest", ["January 2025", "Jan 2025", "2025-01", "December 2024"])
def test_earliest_full_year(report, oldest):
    report["oldestMonth"] = oldest
    assert galevo_annual_metering(report, year=2025) is not None


@pytest.mark.parametrize("oldest", ["February 2025", "2026-01", "2025-13", "unknown", None])
def test_rejects_short_or_unknown_history(report, oldest):
    report["oldestMonth"] = oldest
    assert galevo_annual_metering(report, year=2025) is None


@pytest.mark.parametrize(
    "problem",
    [
        "missing_row",
        "duplicate_row",
        "missing_month",
        "duplicate_month",
        "missing_label",
        "different_labels",
    ],
)
def test_rejects_missing_duplicate_or_mismatched_months(report, problem):
    rows = report["asKwhRaw"]["histogramData"]
    items = rows[0]["items"]
    if problem == "missing_row":
        rows.pop()
    elif problem == "duplicate_row":
        rows.append(rows[0])
    elif problem == "missing_month":
        items.pop()
    elif problem == "duplicate_month":
        items[-1]["x"] = items[0]["x"]
    elif problem == "missing_label":
        del items[-1]["x"]
    else:
        items[-1]["x"] = "Other"
    assert galevo_annual_metering(report, year=2025) is None


@pytest.mark.parametrize("value", [None, False, "2.5", -0.25, float("nan"), float("inf"), 10**500])
def test_rejects_invalid_energy(report, value):
    report["asKwhRaw"]["histogramData"][0]["items"][0]["y"] = value
    assert galevo_annual_metering(report, year=2025) is None


@pytest.mark.parametrize("value", [0, 1e308])
def test_rejects_zero_or_overflowing_electricity_total(report, value):
    for row in report["asKwhRaw"]["histogramData"]:
        if row["tab"] == "ConsumedElectricity":
            for item in row["items"]:
                item["y"] = value
    assert galevo_annual_metering(report, year=2025) is None


def test_intensive_logging_and_rounded_only_are_unavailable(report):
    report["isIntensiveLogsActive"] = True
    assert galevo_annual_metering(report, year=2025) is None
    report["isIntensiveLogsActive"] = False
    report["asKwh"] = report.pop("asKwhRaw")
    assert galevo_annual_metering(report, year=2025) is None
