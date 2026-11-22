"""Native thermostat entities for ELCO Aerotop heating and cooling zones."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .capabilities import supports_native_thermostat
from .const import GALEVO_WRITES_ENABLED, SYSTEM_TYPE_GALEVO
from .control_mapping import (
    zone_hvac_mode,
    zone_hvac_modes,
    zone_mode_for_hvac,
    zone_mode_for_preset,
    zone_preset,
    zone_presets,
)
from .coordinator import ElcoDataUpdateCoordinator
from .entity import ElcoAerotopEntity, async_setup_platform_entities
from .models import NumericVariable, ZoneState


class ElcoZoneClimate(ElcoAerotopEntity, ClimateEntity):
    """Represent one ELCO heating zone as a Home Assistant thermostat."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        zone_number: int,
    ) -> None:
        super().__init__(coordinator, f"zone_{zone_number}_thermostat")
        self._zone_number = zone_number
        self._attr_name = f"Zone {zone_number} thermostat"

    @property
    def _zone(self) -> ZoneState:
        return self.coordinator.data.zones[self._zone_number]

    @property
    def _is_cooling(self) -> bool | None:
        """Return whether ELCO currently has the zone in its cooling season."""
        return self._zone.uses_cooling_setpoints

    @property
    def _target_variable(self) -> NumericVariable:
        """Return the comfort setpoint controlled in the active season."""
        return self._zone.active_comfort_temperature

    @property
    def available(self) -> bool:
        return super().available and supports_native_thermostat(self._zone)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature(0)
        cooling = self._is_cooling
        if (
            cooling is not None
            and not self._target_variable.read_only
            and self._zone.temperature_write_error(cooling=cooling) is None
        ):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self.preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self) -> list[str]:
        return list(zone_presets(self._zone.mode))

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Expose cool when the controller selects cooling setpoints."""
        return [
            HVACMode(mode) for mode in zone_hvac_modes(self._zone.mode, cooling=self._is_cooling)
        ]

    @property
    def min_temp(self) -> float:
        variable = self._target_variable
        return variable.minimum if variable.minimum is not None else 5.0

    @property
    def max_temp(self) -> float:
        variable = self._target_variable
        return variable.maximum if variable.maximum is not None else 35.0

    @property
    def target_temperature_step(self) -> float:
        return self._target_variable.step or 0.5

    @property
    def current_temperature(self) -> float | None:
        """Return a real room reading, never a flow or outdoor substitute."""
        zone = self._zone
        if zone.has_room_sensor is False or zone.room_temperature_error is True:
            return None
        return zone.room_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the active heating or cooling comfort setpoint."""
        return self._target_variable.value

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the native HVAC mode for the current controller mode."""
        mode = zone_hvac_mode(self._zone.mode, cooling=self._is_cooling)
        return HVACMode(mode) if mode is not None else None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the actual controller activity when it is reported."""
        zone = self._zone
        if zone.cooling_active is True:
            return HVACAction.COOLING
        if zone.heating_active is True:
            return HVACAction.HEATING
        if zone.heating_active is not None or zone.cooling_active is not None:
            return HVACAction.IDLE
        return None

    @property
    def preset_mode(self) -> str | None:
        """Return Comfort, eco, or Protection for direct controller modes."""
        return zone_preset(self._zone.mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the zone comfort temperature through the verified write path."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise HomeAssistantError("A target temperature is required")
        cooling = self._is_cooling
        if cooling is None:
            raise HomeAssistantError("Remocon did not report the current plant season")
        await self.coordinator.async_set_zone_temperature(
            self._zone_number,
            "cooling_comfort" if cooling else "comfort",
            float(temperature),
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set Automatic or the best available direct operating mode."""
        try:
            mode = zone_mode_for_hvac(
                self._zone.mode,
                hvac_mode.value,
                cooling=self._is_cooling,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_set_zone_mode(self._zone_number, mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set a verified ELCO direct-heating mode through a native preset."""
        try:
            mode = zone_mode_for_preset(self._zone.mode, preset_mode)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_set_zone_mode(self._zone_number, mode)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up a native thermostat for each safely controllable zone."""
    async_setup_platform_entities(entry, async_add_entities, _create_entities)


def _create_entities(
    coordinator: ElcoDataUpdateCoordinator,
    async_add_entities,
) -> None:
    if not GALEVO_WRITES_ENABLED and coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO:
        return
    entities = []
    for zone_number, zone in coordinator.data.zones.items():
        thermostat = ElcoZoneClimate(coordinator, zone_number)
        if supports_native_thermostat(zone) or thermostat.has_registry_entry("climate"):
            entities.append(thermostat)
    async_add_entities(entities)
