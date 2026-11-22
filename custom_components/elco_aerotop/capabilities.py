"""Capability checks that reject unsupported sentinel values."""

from __future__ import annotations

from typing import Any

from .control_mapping import DHW_MODE_ON, dhw_operations, zone_hvac_modes
from .models import ElcoData, PlantState, ZoneState, galevo_item_writable


def supports_cooling(features: dict[str, Any], zone: ZoneState) -> bool:
    """Return whether cooling is genuinely supported instead of merely zero-filled."""
    return bool(
        features.get("hasTwoCoolingTemp")
        or features.get("distinctHeatCoolSetpoints")
        or zone.cooling_active is True
        or (zone.cooling_comfort_temperature.value or 0) != 0
        or (zone.cooling_reduced_temperature.value or 0) != 0
    )


def supports_room_sensor(zone: ZoneState) -> bool:
    """Return whether a room value should be exposed."""
    return zone.has_room_sensor is True or (
        zone.has_room_sensor is not False and zone.room_temperature is not None
    )


def supports_native_water_heater(plant: PlantState) -> bool:
    """Identify a native control from a comfort setpoint or a mapped operation."""
    return (
        plant.dhw_comfort_temperature.value is not None
        or plant.dhw_always_temperature.value is not None
        or bool(dhw_operations(plant.dhw_mode))
    )


def supports_native_thermostat(zone: ZoneState) -> bool:
    """Identify a native control from a comfort setpoint or a mapped HVAC mode."""
    return zone.active_comfort_temperature.value is not None or bool(zone_hvac_modes(zone.mode))


def supports_scheduled_dhw_comfort_write(data: ElcoData) -> bool:
    """Require Time Based mode and a complete, bounded scheduled setpoint pair."""
    plant = data.plant
    item = data.discovery.system_item("DhwTimeProgComfortTemp")
    comfort = plant.dhw_comfort_temperature
    return (
        plant.dhw_mode.value == DHW_MODE_ON
        and item is not None
        and galevo_item_writable(item)
        and comfort.value is not None
        and comfort.minimum is not None
        and comfort.maximum is not None
        and comfort.minimum <= comfort.value <= comfort.maximum
        and comfort.minimum < comfort.maximum
        and (comfort.step is None or comfort.step > 0)
        and plant.dhw_reduced_temperature.value is not None
    )
