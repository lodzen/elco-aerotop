"""Galevo toggle controls for ELCO Aerotop."""

from __future__ import annotations

from typing import override

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import GALEVO_WRITES_ENABLED, SYSTEM_TYPE_GALEVO
from .coordinator import ElcoDataUpdateCoordinator
from .entity import ElcoAerotopEntity, async_setup_platform_entities
from .models import galevo_item_switch_value, galevo_item_writable


class ElcoGalevoItemSwitch(ElcoAerotopEntity, SwitchEntity):
    """Toggle one writable Galevo boolean data item."""

    def __init__(
        self,
        coordinator: ElcoDataUpdateCoordinator,
        item_id: str,
        key: str,
        name: str,
        *,
        zone_number: int = 0,
    ) -> None:
        super().__init__(coordinator, key)
        self._item_id = item_id
        self._zone_number = zone_number
        self._attr_name = name

    def _item(self) -> dict | None:
        return self.coordinator.data.discovery.system_item(self._item_id, self._zone_number)

    @property
    @override
    def is_on(self) -> bool | None:
        return galevo_item_switch_value(self._item())

    @property
    def available(self) -> bool:
        item = self._item()
        return super().available and bool(
            item and galevo_item_writable(item) and self.is_on is not None
        )

    async def _async_write(self, enabled: bool) -> None:
        await self.coordinator.async_set_galevo_data_item(
            f"{self._item_id}:{self._zone_number}",
            1.0 if enabled else 0.0,
        )

    @override
    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write(True)

    @override
    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write(False)


class ElcoGalevoHolidaySwitch(ElcoAerotopEntity, SwitchEntity):
    """Activate/cancel the plant holiday for the pending date window.

    The ``Holiday`` data item mirrors the controller's holiday flag.
    Turning on sends ``plantData/{gw}/holiday`` with the dates held by
    the ``holiday_start``/``holiday_end`` date entities (APK contract:
    ``ChangeValueDto<LocalDateTime>`` new=start, old=end); turning off
    sends the reset body.
    """

    _attr_name = "Holiday"
    _attr_icon = "mdi:beach"

    def __init__(self, coordinator: ElcoDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "holiday")

    @property
    @override
    def is_on(self) -> bool | None:
        return galevo_item_switch_value(self.coordinator.data.discovery.system_item("Holiday"))

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            key: value.isoformat()
            for key, value in {
                "pending_holiday_start": self.coordinator.pending_holiday_start,
                "pending_holiday_end": self.coordinator.pending_holiday_end,
            }.items()
            if value is not None
        }

    @override
    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_galevo_holiday()

    @override
    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_reset_galevo_holiday()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Galevo toggle controls backed by writable data items."""
    async_setup_platform_entities(entry, async_add_entities, _create_entities)


def _create_entities(
    coordinator: ElcoDataUpdateCoordinator,
    async_add_entities,
) -> None:
    if not GALEVO_WRITES_ENABLED or coordinator.data.discovery.system_type != SYSTEM_TYPE_GALEVO:
        return
    entities: list[SwitchEntity] = []
    for item_id, key, name in (
        ("IsQuite", "quiet_mode", "Quiet mode"),
        ("IsDhwBoost", "domestic_hot_water_boost", "Domestic hot water boost"),
        ("AutomaticThermoregulation", "automatic_thermoregulation", "Automatic thermoregulation"),
    ):
        item = coordinator.data.discovery.system_item(item_id)
        if (
            item is not None
            and galevo_item_writable(item)
            and galevo_item_switch_value(item) is not None
        ):
            entities.append(ElcoGalevoItemSwitch(coordinator, item_id, key, name))
    if galevo_item_switch_value(coordinator.data.discovery.system_item("Holiday")) is not None:
        entities.append(ElcoGalevoHolidaySwitch(coordinator))
    async_add_entities(entities)
