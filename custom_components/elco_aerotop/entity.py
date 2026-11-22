"""Shared ELCO Aerotop entity base."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SYSTEM_TYPE_GALEVO
from .coordinator import ElcoDataUpdateCoordinator


def async_setup_platform_entities(
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    create_entities: Callable[[ElcoDataUpdateCoordinator, AddEntitiesCallback], None],
) -> None:
    """Create entities now, or lazily once the first gateway data arrives.

    The integration fetches its initial Remocon snapshot in the background so
    Home Assistant startup is not blocked; platform setup therefore runs this
    helper, which defers entity creation until ``coordinator.data`` exists.
    """
    coordinator: ElcoDataUpdateCoordinator = entry.runtime_data
    registered: set[str] = set()

    @callback
    def _add_new(new_entities: Iterable[Entity], update_before_add: bool = False) -> None:
        pending = [
            entity
            for entity in new_entities
            if entity.unique_id is not None and entity.unique_id not in registered
        ]
        if pending:
            registered.update(key for entity in pending if (key := entity.unique_id) is not None)
            async_add_entities(pending, update_before_add)

    @callback
    def _discover_entities() -> None:
        if coordinator.unloading or coordinator.data is None:
            return
        create_entities(coordinator, _add_new)

    entry.async_on_unload(coordinator.async_add_listener(_discover_entities))
    _discover_entities()


class ElcoAerotopEntity(CoordinatorEntity[ElcoDataUpdateCoordinator]):
    """Base entity linked to the integration coordinator."""

    _attr_has_entity_name = True

    def has_registry_entry(self, entity_domain: str) -> bool:
        """Return whether this entity already has a persistent Home Assistant ID."""
        registry = er.async_get(self.coordinator.hass)
        return registry.async_get_entity_id(entity_domain, DOMAIN, self._attr_unique_id) is not None

    def __init__(self, coordinator: ElcoDataUpdateCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.api.gateway_id}_{key}"
        metadata = coordinator.data.discovery.plant_metadata
        header = coordinator.data.discovery.plant_header
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.api.gateway_id)},
            manufacturer="ELCO",
            model=str(
                header.get("applianceModel")
                or header.get("ApplianceModel")
                or metadata.get("wheModel")
                or (
                    "Aerotop / Galevo"
                    if coordinator.data.discovery.system_type == SYSTEM_TYPE_GALEVO
                    else "Aerotop / BSB"
                )
            ),
            name=f"ELCO Aerotop {coordinator.api.gateway_id}",
            serial_number=str(metadata.get("gwSerial") or coordinator.api.gateway_id),
            sw_version=(str(metadata["gwFwVer"]) if metadata.get("gwFwVer") else None),
        )
