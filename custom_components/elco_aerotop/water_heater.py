"""Native domestic-hot-water entity for ELCO Aerotop."""

from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .capabilities import supports_native_water_heater
from .const import GALEVO_WRITES_ENABLED, SYSTEM_TYPE_GALEVO
from .control_mapping import (
    DHW_MODE_ON,
    DHW_MODE_PERFORMANCE,
    dhw_mode_for_operation,
    dhw_operation,
    dhw_operations,
)
from .coordinator import ElcoDataUpdateCoordinator
from .entity import ElcoAerotopEntity, async_setup_platform_entities


class ElcoWaterHeater(ElcoAerotopEntity, WaterHeaterEntity):
    """Represent ELCO domestic hot water using Home Assistant's native model."""

    _attr_name = "Domestic hot water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: ElcoDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "domestic_hot_water")
        self._galevo = coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO

    @property
    def available(self) -> bool:
        return super().available and bool(self.supported_features)

    @property
    def operation_list(self) -> list[str]:
        return list(dhw_operations(self.coordinator.data.plant.dhw_mode))

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        plant = self.coordinator.data.plant
        features = WaterHeaterEntityFeature(0)
        scheduled_ready = (
            plant.dhw_comfort_temperature.value is not None
            and plant.dhw_reduced_temperature.value is not None
        )
        if plant.dhw_mode.value is None or (not self._galevo and not scheduled_ready):
            return features

        operations = self.operation_list
        target_ready = scheduled_ready
        if self._galevo:
            target_ready = plant.galevo_dhw_temperature_write_error(comfort=True) is None
            if not target_ready and not plant.dhw_target_temperature.read_only:
                # A combined command can switch to a mode with a usable setpoint.
                target_ready = any(
                    operation in operations
                    and plant.galevo_dhw_temperature_write_error(comfort=True, mode=mode) is None
                    for operation, mode in (
                        (STATE_HEAT_PUMP, DHW_MODE_ON),
                        ("performance", DHW_MODE_PERFORMANCE),
                    )
                )
        if target_ready:
            features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if operations:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        if STATE_OFF in operations and (
            STATE_HEAT_PUMP in operations or self._galevo and "performance" in operations
        ):
            features |= WaterHeaterEntityFeature.ON_OFF
        return features

    def _mode_for_operation(self, operation: str) -> int:
        variable = self.coordinator.data.plant.dhw_mode
        return dhw_mode_for_operation(variable, operation)

    @property
    def current_temperature(self) -> float | None:
        """Return the measured DHW storage temperature when its probe is healthy."""
        plant = self.coordinator.data.plant
        if plant.has_dhw_temperature_probe is False or plant.dhw_temperature_error is True:
            return None
        return plant.dhw_current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the normal DHW comfort target."""
        return self.coordinator.data.plant.dhw_target_temperature.value

    @property
    def min_temp(self) -> float:
        minimum = self.coordinator.data.plant.dhw_target_temperature.minimum
        return minimum if minimum is not None else 5.0

    @property
    def max_temp(self) -> float:
        maximum = self.coordinator.data.plant.dhw_target_temperature.maximum
        return maximum if maximum is not None else 65.0

    @property
    def target_temperature_step(self) -> float | None:
        return self.coordinator.data.plant.dhw_target_temperature.step or 0.5

    @property
    def current_operation(self) -> str | None:
        """Return a stable Home Assistant operation value."""
        return dhw_operation(self.coordinator.data.plant.dhw_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the DHW comfort target and optional operating mode."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            raise HomeAssistantError("A target temperature is required")
        operation = kwargs.get("operation_mode")
        mode = None
        if operation is not None:
            try:
                mode = self._mode_for_operation(str(operation))
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_set_dhw(comfort=float(temperature), mode=mode)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Set a verified ELCO DHW operating mode."""
        try:
            mode = self._mode_for_operation(operation_mode)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_set_dhw(mode=mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn DHW on using the controller's normal On mode."""
        if self._galevo:
            await self.async_set_operation_mode(
                STATE_HEAT_PUMP if STATE_HEAT_PUMP in self.operation_list else "performance"
            )
            return
        await self.async_set_operation_mode(STATE_HEAT_PUMP)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn DHW off using the controller's Off mode."""
        await self.async_set_operation_mode(STATE_OFF)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the native domestic-hot-water entity as capabilities are discovered."""
    async_setup_platform_entities(entry, async_add_entities, _create_entities)


def _create_entities(
    coordinator: ElcoDataUpdateCoordinator,
    async_add_entities,
) -> None:
    if not GALEVO_WRITES_ENABLED and coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO:
        return
    heater = ElcoWaterHeater(coordinator)
    if supports_native_water_heater(coordinator.data.plant) or heater.has_registry_entry(
        "water_heater"
    ):
        async_add_entities([heater])
