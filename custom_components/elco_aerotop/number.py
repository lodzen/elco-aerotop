"""Writable temperatures for ELCO Aerotop."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .bsb_controls import BSB_NUMBER_CONTROL_SPECS, BsbNumberControlSpec, bsb_point_number
from .capabilities import supports_cooling, supports_scheduled_dhw_comfort_write
from .const import (
    GALEVO_DHW_SCHEDULED_COMFORT_WRITE_ENABLED,
    GALEVO_WRITES_ENABLED,
    SYSTEM_TYPE_GALEVO,
)
from .coordinator import ElcoDataUpdateCoordinator
from .entity import ElcoAerotopEntity, async_setup_platform_entities
from .models import ElcoData, NumericVariable, ZoneSetpointSeason, galevo_item_writable


class ElcoTemperatureNumber(ElcoAerotopEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        key: str,
        name: str,
        variable_fn,
        write_fn,
        *,
        available_fn: Callable[[ElcoData], bool] | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._variable_fn = variable_fn
        self._write_fn = write_fn
        self._available_fn = available_fn

    @property
    def _variable(self) -> NumericVariable:
        return self._variable_fn(self.coordinator.data)

    @property
    def native_value(self) -> float | None:
        return self._variable.value

    @property
    def native_min_value(self) -> float:
        return self._variable.minimum if self._variable.minimum is not None else 5.0

    @property
    def native_max_value(self) -> float:
        return self._variable.maximum if self._variable.maximum is not None else 65.0

    @property
    def native_step(self) -> float:
        return self._variable.step if self._variable.step is not None else 0.5

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.native_value is not None
            and (self._available_fn is None or self._available_fn(self.coordinator.data))
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._write_fn(value)


class ElcoBsbNumber(ElcoAerotopEntity, NumberEntity):
    """Control one reviewed scalar from Remocon's BSB settings menu."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        spec: BsbNumberControlSpec,
    ) -> None:
        super().__init__(coordinator, spec.key)
        self._spec = spec
        self._attr_name = spec.name
        if spec.temperature:
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> float | None:
        return bsb_point_number(self.coordinator.data.discovery.bsb_points.get(self._spec.address))

    @property
    def native_min_value(self) -> float:
        return self._spec.minimum

    @property
    def native_max_value(self) -> float:
        maximum = self._spec.maximum
        if self._spec.maximum_address is not None:
            dynamic = bsb_point_number(
                self.coordinator.data.discovery.bsb_points.get(self._spec.maximum_address)
            )
            if dynamic is None and self.coordinator.data.zones:
                dynamic = next(iter(self.coordinator.data.zones.values())).reduced_temperature.value
            if dynamic is not None:
                maximum = min(maximum, dynamic)
        return maximum

    @property
    def native_step(self) -> float:
        return self._spec.step

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_bsb_number(self._spec.key, value)


class ElcoGalevoItemNumber(ElcoAerotopEntity, NumberEntity):
    """One writable Galevo data item exposed as a number.

    Bounds and step follow the controller's item metadata.
    """

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        key: str,
        item_id: str,
        zone_number: int,
        name: str,
        *,
        temperature: bool = True,
    ) -> None:
        super().__init__(coordinator, key)
        self._item_id = item_id
        self._zone_number = zone_number
        self._attr_name = name
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        if temperature:
            self._attr_device_class = NumberDeviceClass.TEMPERATURE
        else:
            self._attr_mode = NumberMode.SLIDER

    def _item(self) -> dict | None:
        return self.coordinator.data.discovery.system_item(self._item_id, self._zone_number)

    @property
    def native_value(self) -> float | None:
        return NumericVariable.parse(self._item()).value

    @property
    def native_min_value(self) -> float:
        minimum = NumericVariable.parse(self._item()).minimum
        return minimum if minimum is not None else 0.0

    @property
    def native_max_value(self) -> float:
        maximum = NumericVariable.parse(self._item()).maximum
        return maximum if maximum is not None else 100.0

    @property
    def native_step(self) -> float:
        step = NumericVariable.parse(self._item()).step
        return step if step is not None and step > 0 else 0.5

    @property
    def available(self) -> bool:
        item = self._item()
        return super().available and item is not None and galevo_item_writable(item)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_galevo_data_item(
            f"{self._item_id}:{self._zone_number}",
            value,
        )


# Galevo per-zone items the APK exposes as user settings (dataItems/set).
# (item id, key/name suffix, temperature unit)
_GALEVO_ZONE_NUMBER_ITEMS: tuple[tuple[str, str, bool], ...] = (
    ("HeatingFlowTemp", "heating_flow_temperature", True),
    ("HeatingFlowOffset", "heating_flow_offset", False),
    ("CoolingFlowTemp", "cooling_flow_temperature", True),
    ("CoolingFlowOffset", "cooling_flow_offset", False),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    async_setup_platform_entities(entry, async_add_entities, _create_entities)


def _zone_temperature_number(
    coordinator: ElcoDataUpdateCoordinator,
    zone_number: int,
    *,
    cooling: bool,
    reduced: bool = False,
    season: ZoneSetpointSeason,
) -> ElcoTemperatureNumber:
    setting = "reduced" if reduced else "comfort"
    label = f"{'cooling ' if cooling else ''}{setting} temperature"
    kind = f"{'cooling_' if cooling else ''}{setting}"
    if season == "inactive":
        label = f"off-season {'cooling' if cooling else 'heating'} {setting} temperature"
        suffix = f"off_season_{'cooling' if cooling else 'heating'}_{setting}_temperature"
    else:
        suffix = f"{kind}_temperature"

    def variable(data: ElcoData) -> NumericVariable:
        return data.zones[zone_number].temperature_variables(cooling=cooling)[int(reduced)]

    def available(data: ElcoData) -> bool:
        zone = data.zones[zone_number]
        return (
            not variable(data).read_only
            and zone.temperature_write_error(cooling=cooling, season=season) is None
        )

    return ElcoTemperatureNumber(
        coordinator,
        f"zone_{zone_number}_{suffix}",
        f"Zone {zone_number} {label}",
        variable,
        lambda value: coordinator.async_set_zone_temperature(
            zone_number, kind, value, season=season
        ),
        available_fn=available if season != "active" else None,
    )


def _create_entities(
    coordinator: ElcoDataUpdateCoordinator,
    async_add_entities,
) -> None:
    entities: list[NumberEntity] = []
    galevo = coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO
    if (
        galevo
        and GALEVO_DHW_SCHEDULED_COMFORT_WRITE_ENABLED
        and supports_scheduled_dhw_comfort_write(coordinator.data)
    ):
        scheduled_comfort = ElcoTemperatureNumber(
            coordinator,
            "dhw_scheduled_comfort_temperature",
            "Scheduled hot water comfort temperature",
            lambda data: data.plant.dhw_comfort_temperature,
            coordinator.async_set_scheduled_dhw_comfort_temperature,
            available_fn=supports_scheduled_dhw_comfort_write,
        )
        if not GALEVO_WRITES_ENABLED or scheduled_comfort.has_registry_entry("number"):
            entities.append(scheduled_comfort)
    if galevo and not GALEVO_WRITES_ENABLED:
        async_add_entities(entities)
        return
    plant = coordinator.data.plant
    if plant.dhw_target_temperature.value is not None:
        dhw_comfort = ElcoTemperatureNumber(
            coordinator,
            "dhw_comfort_temperature",
            "Domestic hot water comfort temperature",
            lambda data: data.plant.dhw_target_temperature,
            lambda value: coordinator.async_set_dhw(comfort=value),
            available_fn=(
                lambda data: data.plant.galevo_dhw_temperature_write_error(comfort=True) is None
            )
            if galevo
            else None,
        )
        if dhw_comfort.has_registry_entry("number"):
            entities.append(dhw_comfort)
    if plant.dhw_reduced_temperature.value is not None:
        entities.append(
            ElcoTemperatureNumber(
                coordinator,
                "dhw_reduced_temperature",
                "Domestic hot water reduced temperature",
                lambda data: data.plant.dhw_reduced_temperature,
                lambda value: coordinator.async_set_dhw(reduced=value),
                available_fn=(
                    lambda data: (
                        data.plant.galevo_dhw_temperature_write_error(comfort=False, reduced=True)
                        is None
                    )
                )
                if galevo
                else None,
            )
        )
    zone_season: ZoneSetpointSeason = "either" if galevo else "active"
    for zone_number, zone in coordinator.data.zones.items():
        has_cooling = supports_cooling(coordinator.data.discovery.features, zone)
        for cooling in (False, True) if has_cooling else (False,):
            comfort, reduced = zone.temperature_variables(cooling=cooling)
            legacy_comfort = _zone_temperature_number(
                coordinator, zone_number, cooling=cooling, season=zone_season
            )
            if comfort.value is not None and legacy_comfort.has_registry_entry("number"):
                entities.append(legacy_comfort)
            if (
                galevo
                and has_cooling
                and not legacy_comfort.has_registry_entry("number", enabled_only=True)
            ):
                off_season = _zone_temperature_number(
                    coordinator, zone_number, cooling=cooling, season="inactive"
                )
                if off_season.has_registry_entry("number") or (
                    comfort.value is not None
                    and reduced.value is not None
                    and not comfort.read_only
                ):
                    entities.append(off_season)
            if reduced.value is not None:
                entities.append(
                    _zone_temperature_number(
                        coordinator, zone_number, cooling=cooling, reduced=True, season=zone_season
                    )
                )

    # BSB data-point controls belong to the BSB family.
    if coordinator.data.zones and coordinator.data.discovery.system_type != SYSTEM_TYPE_GALEVO:
        entities.extend(
            ElcoBsbNumber(coordinator, spec) for spec in BSB_NUMBER_CONTROL_SPECS.values()
        )
    # Galevo data-item numbers (flow temperatures/offsets); item-gated so
    # only what the plant advertises as writable gets an entity.
    if coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO:
        for zone_number in coordinator.data.zones:
            for item_id, suffix, is_temperature in _GALEVO_ZONE_NUMBER_ITEMS:
                item = coordinator.data.discovery.system_item(item_id, zone_number)
                if item is None or not galevo_item_writable(item):
                    continue
                label = suffix.replace("_", " ")
                entities.append(
                    ElcoGalevoItemNumber(
                        coordinator,
                        f"zone_{zone_number}_{suffix}",
                        item_id,
                        zone_number,
                        f"Zone {zone_number} {label}",
                        temperature=is_temperature,
                    )
                )
    async_add_entities(entities)
