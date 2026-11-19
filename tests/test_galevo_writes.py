"""Galevo write guards and enum remapping tests.

All Galevo writes are blocked by ``GALEVO_WRITES_ENABLED`` until the
APK-derived contracts are exercised deliberately — these tests assert the
guard fires before any HTTP request leaves the client.
"""

from __future__ import annotations

import pytest
from test_api import FakeSession

from custom_components.elco_aerotop import api as api_module
from custom_components.elco_aerotop.api import ElcoApiClient
from custom_components.elco_aerotop.control_mapping import (
    dhw_galevo_mode_for_operation,
    dhw_galevo_operation,
    dhw_galevo_operations,
    dhw_operation,
    dhw_operations,
)
from custom_components.elco_aerotop.models import (
    GALEVO_DHW_MODE_TO_RAW,
    GALEVO_ZONE_MODE_TO_RAW,
    PlantState,
    ZoneState,
)


def _client(monkeypatch: pytest.MonkeyPatch | None = None) -> ElcoApiClient:
    if monkeypatch is not None:
        monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", False)
    return ElcoApiClient(FakeSession([]), "user", "pass", "gw", "https://example.test")


def _dhw_mode_item(value: float) -> dict:
    return {
        "DhwMode:0": {
            "id": "DhwMode",
            "zone": 0,
            "value": value,
            "options": [0, 1, 2],
            "optTexts": ["Disabled", "Time Based", "Always active"],
        }
    }


def test_galevo_dhw_time_based_maps_to_on_not_eco() -> None:
    """Galevo DhwMode 1 (Time Based) is the scheduled comfort mode, not eco."""
    plant = PlantState.from_galevo(_dhw_mode_item(1))

    assert plant.dhw_mode.value == 1
    assert dhw_operation(plant.dhw_mode) == "heat_pump"


def test_galevo_dhw_always_active_maps_to_performance() -> None:
    """Galevo DhwMode 2 (Always active) is stronger than the scheduled mode."""
    plant = PlantState.from_galevo(_dhw_mode_item(2))

    assert plant.dhw_mode.value == 3
    assert dhw_operation(plant.dhw_mode) == "performance"
    assert set(dhw_operations(plant.dhw_mode)) == {"off", "heat_pump", "performance"}


def test_galevo_dhw_write_values_translate_back_to_raw() -> None:
    """Canonical select values map back to the raw eDhwMode for writes."""
    plant = PlantState.from_galevo(_dhw_mode_item(1))

    assert GALEVO_DHW_MODE_TO_RAW[plant.dhw_mode.value_for_label("Time Based")] == 1
    assert GALEVO_DHW_MODE_TO_RAW[plant.dhw_mode.value_for_label("Always active")] == 2
    assert GALEVO_DHW_MODE_TO_RAW[plant.dhw_mode.value_for_label("Disabled")] == 0


def test_galevo_water_heater_uses_gateway_labels() -> None:
    """The water heater exposes the gateway's own DHW mode labels."""
    plant = PlantState.from_galevo(_dhw_mode_item(1))

    assert dhw_galevo_operations(plant.dhw_mode) == (
        "Disabled",
        "Time Based",
        "Always active",
    )
    assert dhw_galevo_operation(plant.dhw_mode) == "Time Based"
    assert dhw_galevo_mode_for_operation(plant.dhw_mode, "Always active") == 3
    with pytest.raises(ValueError, match="Unsupported DHW operation"):
        dhw_galevo_mode_for_operation(plant.dhw_mode, "eco")


def test_galevo_zone_mode_write_values_translate_back_to_raw() -> None:
    """Canonical zone-mode values map back to raw eZoneMode for writes."""
    zone = ZoneState.from_galevo(
        1,
        {"mod": {"value": 2, "allowedOptions": [2, 3]}},
        {
            "ZoneMode:1": {
                "id": "ZoneMode",
                "zone": 1,
                "value": 2,
                "options": [2, 3],
                "optTexts": ["Manual", "Time program"],
            }
        },
    )

    assert zone.mode.value_for_label("Manual") == 3  # canonical comfort
    assert zone.mode.value_for_label("Time program") == 1  # canonical automatic
    assert GALEVO_ZONE_MODE_TO_RAW[3] == 2
    assert GALEVO_ZONE_MODE_TO_RAW[1] == 3


@pytest.mark.asyncio
async def test_galevo_writes_raise_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Galevo write is blocked while GALEVO_WRITES_ENABLED is off."""
    client = _client(monkeypatch)

    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_plant_mode(0, features={}, prev_items=[])
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_zone_mode(1, 2, old_mode=3)
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_zone_temperatures(
            1, comfort=21, economy=17, old_comfort=20, old_economy=16
        )
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_dhw_temp(55, old_temperature=53)
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_dhw_mode(2, old_mode=1)
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_dhw_timeprog_temperatures(
            comfort=55, economy=40, old_comfort=53, old_economy=40
        )
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_data_item(
            {"id": "HeatingFlowTemp", "zone": 1, "value": 20.0}, 22.0
        )
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_set_galevo_holiday(
            start="2026-12-20T00:00:00", end="2027-01-04T23:59:59"
        )
    with pytest.raises(ValueError, match="Galevo writes are disabled"):
        await client.async_reset_galevo_holiday(now="2026-11-19T08:00:00")

    assert client._session.calls == []


@pytest.mark.asyncio
async def test_galevo_zone_temperature_dispatch_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The family dispatch validates before reaching the write guard."""
    client = _client(monkeypatch)
    client.last_features_response = {
        "data": {"plant": {"systemType": 3}},
        "features": {},
    }
    zone = ZoneState.from_galevo(
        1,
        {"comf": {"value": 20, "min": 10, "max": 30}, "econ": {"value": 16}},
        {},
    )

    with pytest.raises(ValueError, match="below reduced"):
        await client.async_set_zone_temperatures(zone, comfort=15, reduced=16)

    assert client._session.calls == []
