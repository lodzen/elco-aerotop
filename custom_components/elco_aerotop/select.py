"""Writable modes for ELCO Aerotop."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .bsb_controls import HOLIDAY_OPERATING_LEVEL_ADDRESS, HOLIDAY_OPERATING_LEVEL_OPTIONS
from .const import GALEVO_PLANT_MODE_WRITE_ENABLED, GALEVO_WRITES_ENABLED, SYSTEM_TYPE_GALEVO
from .control_mapping import dhw_modes_are_mapped, zone_modes_are_mapped
from .coordinator import ElcoDataUpdateCoordinator
from .entity import ElcoAerotopEntity, async_setup_platform_entities
from .models import bsb_point_value, galevo_item_writable

_LOGGER = logging.getLogger(__name__)


class ElcoDhwModeSelect(ElcoAerotopEntity, SelectEntity):
    _attr_name = "Domestic hot water mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ElcoDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "dhw_mode")

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.plant.dhw_mode.current_label

    @property
    def options(self) -> list[str]:
        return [option.label for option in self.coordinator.data.plant.dhw_mode.options]

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    async def async_select_option(self, option: str) -> None:
        value = self.coordinator.data.plant.dhw_mode.value_for_label(option)
        await self.coordinator.async_set_dhw(mode=value)


class ElcoZoneModeSelect(ElcoAerotopEntity, SelectEntity):
    """Control a heating-zone mode."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ElcoDataUpdateCoordinator, zone_number: int) -> None:
        super().__init__(coordinator, f"zone_{zone_number}_mode")
        self._zone_number = zone_number
        self._attr_name = f"Zone {zone_number} mode"

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.zones[self._zone_number].mode.current_label

    @property
    def options(self) -> list[str]:
        return [
            option.label for option in self.coordinator.data.zones[self._zone_number].mode.options
        ]

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    async def async_select_option(self, option: str) -> None:
        zone = self.coordinator.data.zones[self._zone_number]
        await self.coordinator.async_set_zone_mode(
            self._zone_number,
            zone.mode.value_for_label(option),
        )


def _plant_mode_options(data) -> list[tuple[int, str]]:
    """Return (value, label) pairs from the PlantMode data item.

    ``optTexts`` pairs positionally with ``options`` (verified on captured
    Galevo items), so every choice the controller advertises is listed.
    """
    item = data.discovery.system_item("PlantMode")
    options = item.get("options") if item else None
    labels = item.get("optTexts") if item else None
    if not isinstance(options, list):
        return []
    result = []
    for index, option in enumerate(options):
        if not isinstance(option, int | float):
            continue
        label = (
            str(labels[index])
            if isinstance(labels, list) and index < len(labels)
            else str(int(option))
        )
        result.append((int(option), label))
    return result


class ElcoPlantModeSelect(ElcoAerotopEntity, SelectEntity):
    """Galevo plant mode with fresh validation and readback."""

    _attr_name = "Plant mode"

    def __init__(self, coordinator: ElcoDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "plant_mode")

    @property
    def current_option(self) -> str | None:
        item = self.coordinator.data.discovery.system_item("PlantMode")
        if not item:
            return None
        return next(
            (
                label
                for value, label in _plant_mode_options(self.coordinator.data)
                if value == item.get("value")
            ),
            None,
        )

    @property
    def options(self) -> list[str]:
        return [label for _, label in _plant_mode_options(self.coordinator.data)]

    @property
    def available(self) -> bool:
        item = self.coordinator.data.discovery.system_item("PlantMode")
        return super().available and bool(item and galevo_item_writable(item) and self.options)

    async def async_select_option(self, option: str) -> None:
        value = next(
            (
                value
                for value, label in _plant_mode_options(self.coordinator.data)
                if label == option
            ),
            None,
        )
        if value is None:
            raise ValueError(f"Unsupported plant mode: {option}")
        _LOGGER.debug("Plant mode select: %r -> value %s", option, value)
        await self.coordinator.async_set_plant_mode(value)


class ElcoGalevoItemSelect(ElcoAerotopEntity, SelectEntity):
    """One writable Galevo enum data item (options + optTexts).

    Options and labels come straight from the item's ``options``/
    ``optTexts`` pair; ``fallback_labels`` supplies names for items the
    gateway returns without ``optTexts``. The write goes through
    ``dataItems/{gw}/set`` and stays blocked by ``GALEVO_WRITES_ENABLED``.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        item_id: str,
        zone_number: int,
        name: str,
        *,
        fallback_labels: dict[int, str] | None = None,
    ) -> None:
        suffix = item_id.lower()
        super().__init__(coordinator, f"zone_{zone_number}_{suffix}")
        self._item_id = item_id
        self._zone_number = zone_number
        self._attr_name = name
        self._fallback_labels = fallback_labels or {}

    def _item(self) -> dict | None:
        return self.coordinator.data.discovery.system_item(self._item_id, self._zone_number)

    def _options(self) -> list[tuple[int, str]]:
        item = self._item()
        options = item.get("options") if item else None
        labels = item.get("optTexts") if item else None
        if not isinstance(options, list):
            return []
        result = []
        for index, option in enumerate(options):
            if not isinstance(option, int | float):
                continue
            raw = int(option)
            if isinstance(labels, list) and index < len(labels):
                label = str(labels[index])
            else:
                label = self._fallback_labels.get(raw, str(raw))
            result.append((raw, label))
        return result

    @property
    def current_option(self) -> str | None:
        item = self._item()
        if not item:
            return None
        return next(
            (label for value, label in self._options() if value == item.get("value")),
            None,
        )

    @property
    def options(self) -> list[str]:
        return [label for _, label in self._options()]

    async def async_select_option(self, option: str) -> None:
        value = next(
            (value for value, label in self._options() if label == option),
            None,
        )
        if value is None:
            raise ValueError(f"Unsupported option: {option}")
        await self.coordinator.async_set_galevo_data_item(
            f"{self._item_id}:{self._zone_number}",
            value,
        )


class ElcoHolidayOperatingLevelSelect(ElcoAerotopEntity, SelectEntity):
    """Control the BSB heating-circuit 1 holiday operating level."""

    _attr_name = "Zone 1 holiday operating level"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(HOLIDAY_OPERATING_LEVEL_OPTIONS)

    def __init__(self, coordinator: ElcoDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "zone_1_holiday_operating_level")

    @property
    def current_option(self) -> str | None:
        label = bsb_point_value(
            self.coordinator.data.discovery.bsb_points.get(HOLIDAY_OPERATING_LEVEL_ADDRESS)
        )
        if isinstance(label, str):
            normalized = label.casefold()
            if "reduced" in normalized:
                return "Reduced"
            if "frost" in normalized or "protection" in normalized:
                return "Frost protection"
        zone = self.coordinator.data.zones.get(1)
        if zone is None or zone.use_reduced_operation_mode_on_holiday is None:
            return None
        return "Reduced" if zone.use_reduced_operation_mode_on_holiday else "Frost protection"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_holiday_operating_level(option)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    async_setup_platform_entities(entry, async_add_entities, _create_entities)


def _create_entities(
    coordinator: ElcoDataUpdateCoordinator,
    async_add_entities,
) -> None:
    entities: list[SelectEntity] = []
    galevo = coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO
    if not galevo or GALEVO_WRITES_ENABLED:
        dhw_select = ElcoDhwModeSelect(coordinator)
        if not dhw_modes_are_mapped(
            coordinator.data.plant.dhw_mode
        ) or dhw_select.has_registry_entry("select"):
            entities.append(dhw_select)
        for zone_number, zone in coordinator.data.zones.items():
            zone_select = ElcoZoneModeSelect(coordinator, zone_number)
            if not zone_modes_are_mapped(zone.mode) or zone_select.has_registry_entry("select"):
                entities.append(zone_select)
    # Galevo plant-mode select; options follow the controller's PlantMode item.
    # AutomaticThermoregulation is a boolean item and lives in switch.py
    # (control section) rather than as a two-option select.
    item = coordinator.data.discovery.system_item("PlantMode")
    if (
        galevo
        and GALEVO_PLANT_MODE_WRITE_ENABLED
        and item
        and galevo_item_writable(item)
        and _plant_mode_options(coordinator.data)
    ):
        entities.append(ElcoPlantModeSelect(coordinator))
    # Holiday operating level is a BSB data point; no Galevo equivalent.
    if 1 in coordinator.data.zones and not galevo:
        entities.append(ElcoHolidayOperatingLevelSelect(coordinator))
    async_add_entities(entities)
